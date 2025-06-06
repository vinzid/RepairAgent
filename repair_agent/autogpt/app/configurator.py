"""Configurator module."""
from __future__ import annotations

from typing import Literal

import click
from colorama import Back, Fore, Style

from autogpt import utils
from autogpt.config import Config
from autogpt.config.config import GPT_3_MODEL, GPT_4_MODEL
from autogpt.llm.api_manager import ApiManager
from autogpt.logs import logger
from autogpt.memory.vector import get_supported_memory_backends


def create_config(
    config: Config,
    continuous: bool,
    continuous_limit: int,
    ai_settings_file: str,
    prompt_settings_file: str,
    skip_reprompt: bool,
    speak: bool,
    debug: bool,
    gpt3only: bool,
    gpt4only: bool,
    memory_type: str,
    browser_name: str,
    allow_downloads: bool,
    skip_news: bool,
    model: Optional[str] = None,   # NEW!
) -> None:
    """Updates the config object with the given arguments.

    Args:
        continuous (bool): Whether to run in continuous mode
        continuous_limit (int): The number of times to run in continuous mode
        ai_settings_file (str): The path to the ai_settings.yaml file
        prompt_settings_file (str): The path to the prompt_settings.yaml file
        skip_reprompt (bool): Whether to skip the re-prompting messages at the beginning of the script
        speak (bool): Whether to enable speak mode
        debug (bool): Whether to enable debug mode
        gpt3only (bool): Whether to enable GPT3.5 only mode
        gpt4only (bool): Whether to enable GPT4 only mode
        memory_type (str): The type of memory backend to use
        browser_name (str): The name of the browser to use when using selenium to scrape the web
        allow_downloads (bool): Whether to allow Auto-GPT to download files natively
        skips_news (bool): Whether to suppress the output of latest news on startup
    """
    config.debug_mode = False
    config.continuous_mode = False
    config.speak_mode = False

    if debug:
        logger.typewriter_log("Debug Mode: ", Fore.GREEN, "ENABLED")
        config.debug_mode = True

    if continuous:
        logger.typewriter_log("Continuous Mode: ", Fore.RED, "ENABLED")
        logger.typewriter_log(
            "WARNING: ",
            Fore.RED,
            "Continuous mode is not recommended. It is potentially dangerous and may"
            " cause your AI to run forever or carry out actions you would not usually"
            " authorise. Use at your own risk.",
        )
        config.continuous_mode = True

        if continuous_limit:
            logger.typewriter_log(
                "Continuous Limit: ", Fore.GREEN, f"{continuous_limit}"
            )
            config.continuous_limit = continuous_limit

    # Check if continuous limit is used without continuous mode
    if continuous_limit and not continuous:
        raise click.UsageError("--continuous-limit can only be used with --continuous")

    if speak:
        logger.typewriter_log("Speak Mode: ", Fore.GREEN, "ENABLED")
        config.speak_mode = True

    # Set the default LLM models
    if model:
        config.fast_llm = model
        config.smart_llm = model
        logger.typewriter_log(f"Model override: Using {model} for both fast_llm and smart_llm.", Fore.GREEN)
    elif gpt3only:
        logger.typewriter_log("GPT3.5 Only Mode: ", Fore.GREEN, "ENABLED")
        config.fast_llm = GPT_3_MODEL
        config.smart_llm = GPT_3_MODEL
    elif (
        gpt4only
        and check_model(GPT_4_MODEL, model_type="smart_llm", config=config)
        == GPT_4_MODEL
    ):
        logger.typewriter_log("GPT4 Only Mode: ", Fore.GREEN, "ENABLED")
        config.fast_llm = GPT_4_MODEL
        config.smart_llm = GPT_4_MODEL
    else:
        config.fast_llm = check_model(config.fast_llm, "fast_llm", config=config)
        config.smart_llm = check_model(config.smart_llm, "smart_llm", config=config)
        
    if memory_type:
        supported_memory = get_supported_memory_backends()
        chosen = memory_type
        if chosen not in supported_memory:
            logger.typewriter_log(
                "ONLY THE FOLLOWING MEMORY BACKENDS ARE SUPPORTED: ",
                Fore.RED,
                f"{supported_memory}",
            )
            logger.typewriter_log("Defaulting to: ", Fore.YELLOW, config.memory_backend)
        else:
            config.memory_backend = chosen

    if skip_reprompt:
        logger.typewriter_log("Skip Re-prompt: ", Fore.GREEN, "ENABLED")
        config.skip_reprompt = True

    if ai_settings_file:
        file = ai_settings_file

        # Validate file
        (validated, message) = utils.validate_yaml_file(file)
        if not validated:
            logger.typewriter_log("FAILED FILE VALIDATION", Fore.RED, message)
            logger.double_check()
            exit(1)

        logger.typewriter_log("Using AI Settings File:", Fore.GREEN, file)
        config.ai_settings_file = file
        config.skip_reprompt = True

    if prompt_settings_file:
        file = prompt_settings_file

        # Validate file
        (validated, message) = utils.validate_yaml_file(file)
        if not validated:
            logger.typewriter_log("FAILED FILE VALIDATION", Fore.RED, message)
            logger.double_check()
            exit(1)

        logger.typewriter_log("Using Prompt Settings File:", Fore.GREEN, file)
        config.prompt_settings_file = file

    if browser_name:
        config.selenium_web_browser = browser_name

    if allow_downloads:
        logger.typewriter_log("Native Downloading:", Fore.GREEN, "ENABLED")
        logger.typewriter_log(
            "WARNING: ",
            Fore.YELLOW,
            f"{Back.LIGHTYELLOW_EX}Auto-GPT will now be able to download and save files to your machine.{Back.RESET} "
            + "It is recommended that you monitor any files it downloads carefully.",
        )
        logger.typewriter_log(
            "WARNING: ",
            Fore.YELLOW,
            f"{Back.RED + Style.BRIGHT}ALWAYS REMEMBER TO NEVER OPEN FILES YOU AREN'T SURE OF!{Style.RESET_ALL}",
        )
        config.allow_downloads = True

    if skip_news:
        config.skip_news = True


def check_model(
    model_name: str,
    model_type: Literal["smart_llm", "fast_llm"],
    config: Config,
) -> str:
    """Check if model is available for use. If not, return gpt-3.5-turbo."""
    openai_credentials = config.get_openai_credentials(model_name)
    api_manager = ApiManager()
    models = api_manager.get_models(**openai_credentials)

    if any(model_name in m["id"] for m in models):
        return model_name

    logger.typewriter_log(
        "WARNING: ",
        Fore.YELLOW,
        f"You do not have access to {model_name}. Setting {model_type} to "
        f"gpt-3.5-turbo.",
    )
    return "gpt-3.5-turbo"
