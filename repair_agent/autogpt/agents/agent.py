from __future__ import annotations

import json
import json_repair
import time
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from autogpt.config import AIConfig, Config
    from autogpt.llm.base import ChatModelResponse, ChatSequence
    from autogpt.memory.vector import VectorMemory
    from autogpt.models.command_registry import CommandRegistry

from autogpt.json_utils.utilities import extract_dict_from_response, validate_dict
from autogpt.llm.api_manager import ApiManager
from autogpt.llm.base import Message
from autogpt.llm.utils import count_string_tokens
from autogpt.logs import logger
from autogpt.logs.log_cycle import (
    CURRENT_CONTEXT_FILE_NAME,
    FULL_MESSAGE_HISTORY_FILE_NAME,
    NEXT_ACTION_FILE_NAME,
    USER_INPUT_FILE_NAME,
    LogCycleHandler,
)
from autogpt.workspace import Workspace
from autogpt.commands.defects4j_static import query_for_mutants, construct_fix_command, get_detailed_list_of_buggy_lines

from .base import AgentThoughts, BaseAgent, CommandArgs, CommandName


class Agent(BaseAgent):
    """Agent class for interacting with Auto-GPT."""

    def __init__(
        self,
        ai_config: AIConfig,
        command_registry: CommandRegistry,
        memory: VectorMemory,
        triggering_prompt: str,
        config: Config,
        cycle_budget: Optional[int] = None,
        experiment_file: str = None
    ):
        super().__init__(
            ai_config=ai_config,
            command_registry=command_registry,
            config=config,
            default_cycle_instruction=triggering_prompt,
            cycle_budget=cycle_budget,
            experiment_file = experiment_file
        )

        self.memory = memory
        """VectorMemoryProvider used to manage the agent's context (TODO)"""

        self.workspace = Workspace(config.workspace_path, config.restrict_to_workspace)
        """Workspace that the agent has access to, e.g. for reading/writing files."""

        self.created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
        """Timestamp the agent was created; only used for structured debug logging."""

        self.log_cycle_handler = LogCycleHandler()
        """LogCycleHandler for structured debug logging."""

    def construct_base_prompt(self, *args, **kwargs) -> ChatSequence:
        if kwargs.get("prepend_messages") is None:
            kwargs["prepend_messages"] = []


        # Add budget information (if any) to prompt
        api_manager = ApiManager()
        if api_manager.get_total_budget() > 0.0:
            remaining_budget = (
                api_manager.get_total_budget() - api_manager.get_total_cost()
            )
            if remaining_budget < 0:
                remaining_budget = 0

            budget_msg = Message(
                "system",
                f"Your remaining API budget is ${remaining_budget:.3f}"
                + (
                    " BUDGET EXCEEDED! SHUT DOWN!\n\n"
                    if remaining_budget == 0
                    else " Budget very nearly exceeded! Shut down gracefully!\n\n"
                    if remaining_budget < 0.005
                    else " Budget nearly exceeded. Finish up.\n\n"
                    if remaining_budget < 0.01
                    else ""
                ),
            )
            logger.debug(budget_msg)

            if kwargs.get("append_messages") is None:
                kwargs["append_messages"] = []
            kwargs["append_messages"].append(budget_msg)

        return super().construct_base_prompt(*args, **kwargs)

    def on_before_think(self, *args, **kwargs) -> ChatSequence:
        prompt = super().on_before_think(*args, **kwargs)

        self.log_cycle_handler.log_count_within_cycle = 0
        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            self.history.raw(),
            FULL_MESSAGE_HISTORY_FILE_NAME,
        )
        self.log_cycle_handler.log_cycle(
            self.project_name+"_"+self.bug_index,
            self.created_at,
            self.cycle_count,
            prompt.raw(),
            CURRENT_CONTEXT_FILE_NAME,
        )
        return prompt

    def execute(
        self,
        command_name: str | None,
        command_args: dict[str, str] | None,
        user_input: str | None,
    ) -> str:
        # Execute command
        if command_name is not None and command_name.lower().startswith("error"):
            result = f"Could not execute command: {command_name}{command_args}"
        elif command_name == "human_feedback":
            result = f"Human feedback: {user_input}"
            self.log_cycle_handler.log_cycle(
                self.ai_config.ai_name,
                self.created_at,
                self.cycle_count,
                user_input,
                USER_INPUT_FILE_NAME,
            )

        else:
            for plugin in self.config.plugins:
                if not plugin.can_handle_pre_command():
                    continue
                command_name, arguments = plugin.pre_command(command_name, command_args)
            command_result = execute_command(
                command_name=command_name,
                arguments=command_args,
                agent=self,
            )
            result = f"Command {command_name} returned: " f"{command_result}"
            result_tlength = count_string_tokens(str(command_result), self.llm.name)
            memory_tlength = count_string_tokens(
                str(self.history.summary_message()), self.llm.name
            )
            if result_tlength + memory_tlength > self.send_token_limit:
                result = f"Failure: command {command_name} returned too much output. \
                    Do not execute this command again with the same arguments."

            for plugin in self.config.plugins:
                if not plugin.can_handle_post_command():
                    continue
                result = plugin.post_command(command_name, result)
        # Check if there's a result from the command append it to the message
        if result is None:
            self.history.add("user", "Unable to execute command", "action_result")
        else:
            self.history.add("user", result, "action_result")

        return result


    def parse_and_process_response(
        self, llm_response: ChatModelResponse, *args, **kwargs
    ) -> tuple[CommandName | None, CommandArgs | None, AgentThoughts]:
        if not llm_response.content:
            raise SyntaxError("Assistant response has no text content")

        exps = self.exps
        with open(os.path.join("experimental_setups", exps[-1], "responses", "model_responses_{}_{}".format(self.project_name, self.bug_index)), "a+") as patf:
            patf.write(llm_response.content)
        assistant_reply_dict = extract_dict_from_response(llm_response.content)

        if not isinstance(assistant_reply_dict, dict):
            raise SyntaxError(
                "assistant_reply_dict is not dict"
            )

        if "command" not in assistant_reply_dict:
            assistant_reply_dict["command"] = {"name": "missing_command", "args":{}}
        command_dict = assistant_reply_dict["command"]

        with open("commands_interface.json") as cif:
            commands_interface = json.load(cif)

        if command_dict.get("name", "") in list(commands_interface.keys()):
            ref_args = commands_interface[command_dict["name"]]
            if isinstance(command_dict.get("args", None), dict):
                command_args = list(command_dict["args"].keys())
                new_command_dict = {"name": command_dict["name"], "args":{}}
                for k in command_args:
                    if k in ref_args:
                        new_command_dict["args"][k] = command_dict["args"][k]
                
                unmatched_args = [arg for arg in command_args if arg not in ref_args]
                unmatched_ref = [arg for arg in ref_args if arg not in list(new_command_dict["args"].keys())]

                for uarg in unmatched_args:
                    for uref in unmatched_ref:
                        if uarg in uref:
                            new_command_dict["args"][uref] = command_dict["args"][uarg]
                            break
                
                if "project_name" in new_command_dict["args"]:
                    if "_" in new_command_dict["args"]["project_name"]:
                        name_only = new_command_dict["args"]["project_name"].split("_")[0]
                        new_command_dict["args"]["project_name"] = name_only
                if new_command_dict["name"] in [
                    "write_fix", 
                    "try_fixes", 
                    "read_range", 
                    "search_code_base", 
                    "get_classes_and_methods",
                    "extract_similar_functions_calls",
                    "extract_method_code",
                    "extract_test_code"]:
                    new_command_dict["args"]["project_name"] = self.project_name
                    new_command_dict["args"]["bug_index"] = self.bug_index

                assistant_reply_dict["command"] = new_command_dict
            else:
                assistant_reply_dict["command"] = {"name": "unknown_command", "args":{}}
            
            if assistant_reply_dict["command"]["name"] == "write_fix":
                try:
                    fix_content = assistant_reply_dict["command"]["args"].get("changes_dicts", "[]")
                except Exception as e:
                    fix_content = "No fix suggested yet."
                    logger.info("NO FIX WAS SUGGESTED"+ str(e))
                # getting the list of buggy lines
                detailed_buggies = get_detailed_list_of_buggy_lines(self.project_name, self.bug_index)
                
                # create mutation prompt
                mutant_prompt = self.construct_mutation_prompt(fix_content, detailed_buggies)
                # save mutation prompt
                with open(os.path.join("experimental_setups", exps[-1], "mutations_history", "mutations_prompt_{}_{}".format(self.project_name, self.bug_index)), "a") as mph:
                    mph.write(mutant_prompt)
                
                # Asking main agent for mutants
                print("-----start query_for_mutants-----", datetime.now())
                mutants = query_for_mutants(mutant_prompt)
                print("-----end query_for_mutants-----", datetime.now())

                matched_mutants = re.findall('```(?:json)?(.*?)```', mutants, flags=re.M | re.DOTALL)
                if len(matched_mutants) == 0:
                    matched_mutants.append(mutants)
                mutants_json = []
                for mutant in matched_mutants:
                    mutant = json_repair.loads(mutant)
                    if isinstance(mutant, dict):
                        mutants_json.append(mutant)
                    if isinstance(mutant, list):
                        mutants_json.extend(mutant)
                
                exps = self.exps
                existing_mutants = []
                mutants_save_path = os.path.join("experimental_setups", exps[-1], "mutations_history", "mutants_{}_{}.json".format(self.project_name, self.bug_index))
                
                if os.path.exists(mutants_save_path):
                    with open(mutants_save_path) as json_file:
                        existing_mutants = json.load(json_file)
                with open(os.path.join("experimental_setups", exps[-1], "mutations_history", "mutants_raw_{}_{}.json".format(self.project_name, self.bug_index)), "a") as raw_m:
                    raw_m.write(mutants)
                
                try:
                    self.save_to_json(mutants_save_path, mutants_json)
                    logger.info("MUTANTS LENGTH: " + str(len(mutants_json)) + "\n\n")
                    
                    for m in mutants_json:
                        if m not in existing_mutants:
                            fix_command = construct_fix_command(m, self.project_name, self.bug_index)
                            if isinstance(fix_command, str):
                                logger.info("MUTANT OBJECT: " + fix_command + "\n\n")
                                raise TypeError("Error: EXPECTED 'DICT', RECEIEVED 'STR' INSTEAD" + fix_command)
                            name, args = extract_command(fix_command, None, self.config)
                            
                            exec_result = execute_command(name, args, self)
                            logger.info("---------------------------\nRESULT OF TRYING {} returned\n {} \n----------------------------\n\n".format(args, exec_result))
                            if " 0 failing test" in exec_result:
                                logger.info("PLAUSIBLE PATCH FOUND. REASON = 0 FAILING TESTS.\n\n")
                                ## writing the plausible patch
                                with open(os.path.join("experimental_setups", exps[-1], "plausible_patches", "plausible_patches_{}_{}.json".format(self.project_name, self.bug_index)), "a+") as exps:
                                    exps.write("### PLAUSIBLE FIX\n{}\n".format(str(m)))
                except Exception as e:
                    logger.info("Error in loading the mutants response: " + str(e) + "\n\n")

        valid, errors = validate_dict(assistant_reply_dict, self.config)
        
        if not valid:
            raise SyntaxError(
                "Validation of response failed:\n  "
                + ";\n  ".join([str(e) for e in errors])
            )

        for plugin in self.config.plugins:
            if not plugin.can_handle_post_planning():
                continue
            assistant_reply_dict = plugin.post_planning(assistant_reply_dict)

        response = None, None, assistant_reply_dict

        # Print Assistant thoughts
        if assistant_reply_dict != {}:
            # Get command name and arguments
            try:
                command_name, arguments = extract_command(
                    assistant_reply_dict, llm_response, self.config
                )
                response = command_name, arguments, assistant_reply_dict
            except Exception as e:
                logger.error("Error: \n", str(e))

        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            assistant_reply_dict,
            NEXT_ACTION_FILE_NAME,
        )
        return response


def extract_command(
    assistant_reply_json: dict, assistant_reply: ChatModelResponse, config: Config
) -> tuple[str, dict[str, str]]:
    """Parse the response and return the command name and arguments

    Args:
        assistant_reply_json (dict): The response object from the AI
        assistant_reply (ChatModelResponse): The model response from the AI
        config (Config): The config object

    Returns:
        tuple: The command name and arguments

    Raises:
        json.decoder.JSONDecodeError: If the response is not valid JSON

        Exception: If any other error occurs
    """
    if config.openai_functions:
        if assistant_reply.function_call is None:
            return "Error:", {"message": "No 'function_call' in assistant reply"}
        assistant_reply_json["command"] = {
            "name": assistant_reply.function_call.name,
            "args": json.loads(assistant_reply.function_call.arguments),
        }
    try:
        if "command" not in assistant_reply_json:
            return "Error:", {"message": "Missing 'command' object in JSON"}

        if not isinstance(assistant_reply_json, dict):
            return (
                "Error:",
                {
                    "message": f"The previous message sent was not a dictionary {assistant_reply_json}"
                },
            )

        command = assistant_reply_json["command"]
        if not isinstance(command, dict):
            return "Error:", {"message": "'command' object is not a dictionary"}

        if "name" not in command:
            return "Error:", {"message": "Missing 'name' field in 'command' object"}

        command_name = command["name"]

        # Use an empty dictionary if 'args' field is not present in 'command' object
        arguments = command.get("args", {})

        return command_name, arguments
    except json.decoder.JSONDecodeError:
        return "Error:", {"message": "Invalid JSON"}
    # All other errors, return "Error: + error message"
    except Exception as e:
        return "Error:", {"message": str(e)}


def execute_command(
    command_name: str,
    arguments: dict[str, str],
    agent: Agent,
) -> Any:
    """Execute the command and return the result

    Args:
        command_name (str): The name of the command to execute
        arguments (dict): The arguments for the command
        agent (Agent): The agent that is executing the command

    Returns:
        str: The result of the command
    """
    try:
        # Execute a native command with the same name or alias, if it exists
        if command := agent.command_registry.get_command(command_name):
            return command(**arguments, agent=agent)

        # Handle non-native commands (e.g. from plugins)
        for command in agent.ai_config.prompt_generator.commands:
            if (
                command_name == command.label.lower()
                or command_name == command.name.lower()
            ):
                return command.function(**arguments)

        raise RuntimeError(
            f"Cannot execute '{command_name}': unknown command."
            " Do not try to use this command again."
        )
    except Exception as e:
        return f"Error: {str(e)}"
