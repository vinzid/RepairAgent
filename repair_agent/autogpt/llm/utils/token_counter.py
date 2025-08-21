"""Functions for counting the number of tokens in a message or string."""
from __future__ import annotations

from typing import List, overload

import re
import tiktoken

from autogpt.llm.base import Message
from autogpt.logs import logger


def get_encoding(model_name):
    try:
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        encoding_name = "cl100k_base" if model_name in ["gpt-3.5-turbo", "gpt-4"] else "o200k_base"
        logger.warn(f"Warning: encoding for {model_name} not found. Using {encoding_name} encoding.")
        encoding = tiktoken.get_encoding(encoding_name)
    return encoding
        

@overload
def count_message_tokens(messages: Message, model: str = "gpt-3.5-turbo") -> int:
    ...


@overload
def count_message_tokens(messages: List[Message], model: str = "gpt-3.5-turbo-0125") -> int:
    ...


def count_message_tokens(
    messages: Message | List[Message], model: str = "gpt-3.5-turbo-0125"
) -> int:
    """
    Returns the number of tokens used by a list of messages.

    Args:
        messages (list): A list of messages, each of which is a dictionary
            containing the role and content of the message.
        model (str): The name of the model to use for tokenization.
            Defaults to "gpt-3.5-turbo-0125".

    Returns:
        int: The number of tokens used by the list of messages.
    """
    if isinstance(messages, Message):
        messages = [messages]

    if model.startswith("gpt-3.5-turbo"):
        tokens_per_message = (
            4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        )
        tokens_per_name = -1  # if there's a name, the role is omitted
        encoding_model = "gpt-3.5-turbo"
    elif model.startswith("gpt-4"):
        tokens_per_message = 3
        tokens_per_name = 1
        encoding_model = re.sub(r"(gpt-4(o|\.\d+)?).*", "\\1", model)
    else:
        return 200
        raise NotImplementedError(
            f"count_message_tokens() is not implemented for model {model}.\n"
            " See https://github.com/openai/openai-python/blob/main/chatml.md for"
            " information on how messages are converted to tokens."
        )
    encoding = get_encoding(encoding_model)

    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.raw().items():
            num_tokens += len(encoding.encode(value))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens


def count_string_tokens(string: str, model_name: str) -> int:
    """
    Returns the number of tokens in a text string.

    Args:
        string (str): The text string.
        model_name (str): The name of the encoding to use. (e.g., "gpt-3.5-turbo")

    Returns:
        int: The number of tokens in the text string.
    """
    try:
        encoding = get_encoding(model_name)
    except:
        return 200
    return len(encoding.encode(string))
