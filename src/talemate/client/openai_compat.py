import urllib
import random
import pydantic
import structlog
from openai import AsyncOpenAI, NotFoundError, PermissionDeniedError

from talemate.client.base import ClientBase, ExtraField
from talemate.client.registry import register
from talemate.config import Client as BaseClientConfig
from talemate.emit import emit

log = structlog.get_logger("talemate.client.openai_compat")

EXPERIMENTAL_DESCRIPTION = """Use this client if you want to connect to a service implementing an OpenAI-compatible API. Success is going to depend on the level of compatibility. Use the actual OpenAI client if you want to connect to OpenAI's API."""


class Defaults(pydantic.BaseModel):
    api_url: str = "http://localhost:5000"
    api_key: str = ""
    max_token_length: int = 8192
    model: str = ""
    api_handles_prompt_template: bool = False


class ClientConfig(BaseClientConfig):
    api_handles_prompt_template: bool = False


@register()
class OpenAICompatibleClient(ClientBase):
    client_type = "openai_compat"
    conversation_retries = 0
    config_cls = ClientConfig

    class Meta(ClientBase.Meta):
        title: str = "OpenAI Compatible API"
        name_prefix: str = "OpenAI Compatible API"
        experimental: str = EXPERIMENTAL_DESCRIPTION
        enable_api_auth: bool = True
        manual_model: bool = True
        defaults: Defaults = Defaults()
        extra_fields: dict[str, ExtraField] = {
            "api_handles_prompt_template": ExtraField(
                name="api_handles_prompt_template",
                type="bool",
                label="API Handles Prompt Template",
                required=False,
                description="The API handles the prompt template, meaning your choice in the UI for the prompt template below will be ignored.",
            )
        }

    def __init__(
        self, model=None, api_key=None, api_handles_prompt_template=False, **kwargs
    ):
        self.model_name = model
        self.api_key = api_key
        self.api_handles_prompt_template = api_handles_prompt_template
        super().__init__(**kwargs)

    @property
    def experimental(self):
        return EXPERIMENTAL_DESCRIPTION

    @property
    def can_be_coerced(self):
        """
        Determines whether or not his client can pass LLM coercion. (e.g., is able
        to predefine partial LLM output in the prompt)
        """
        return not self.api_handles_prompt_template

    def set_client(self, **kwargs):
        self.api_key = kwargs.get("api_key", self.api_key)
        self.api_handles_prompt_template = kwargs.get(
            "api_handles_prompt_template", self.api_handles_prompt_template
        )
        url = self.api_url
        self.client = AsyncOpenAI(base_url=url, api_key=self.api_key)
        self.model_name = (
            kwargs.get("model") or kwargs.get("model_name") or self.model_name
        )

    def tune_prompt_parameters(self, parameters: dict, kind: str):
        super().tune_prompt_parameters(parameters, kind)

        if "repetition_penalty" in parameters:
            parameters["presence_penalty"] = parameters.pop(
                "repetition_penalty"
            )

        allowed_params = ["max_tokens", "presence_penalty", "top_p", "temperature"]

        # drop unsupported params
        for param in list(parameters.keys()):
            if param not in allowed_params:
                del parameters[param]

    def prompt_template(self, system_message: str, prompt: str):

        log.debug(
            "IS API HANDLING PROMPT TEMPLATE",
            api_handles_prompt_template=self.api_handles_prompt_template,
        )

        if not self.api_handles_prompt_template:
            return super().prompt_template(system_message, prompt)

        if "<|BOT|>" in prompt:
            _, right = prompt.split("<|BOT|>", 1)
            if right:
                prompt = prompt.replace("<|BOT|>", "\nStart your response with: ")
            else:
                prompt = prompt.replace("<|BOT|>", "")

        return prompt

    async def get_model_name(self):
        return self.model_name

    async def generate(self, prompt: str, parameters: dict, kind: str):
        """
        Generates text from the given prompt and parameters.
        """
        human_message = {"role": "user", "content": prompt.strip()}

        self.log.debug("generate", prompt=prompt[:128] + " ...", parameters=parameters)
        
        parameters["prompt"] = prompt

        try:
            response = await self.client.completions.create(
                model=self.model_name, **parameters
            )

            log.debug("generate response", response=response)
            return response.choices[0].text
        except PermissionDeniedError as e:
            self.log.error("generate error", e=e)
            emit("status", message="Client API: Permission Denied", status="error")
            return ""
        except Exception as e:
            self.log.error("generate error", e=e)
            emit(
                "status", message="Error during generation (check logs)", status="error"
            )
            return ""

    def reconfigure(self, **kwargs):
        if kwargs.get("model"):
            self.model_name = kwargs["model"]
        if "api_url" in kwargs:
            self.api_url = kwargs["api_url"]
        if "max_token_length" in kwargs:
            self.max_token_length = (
                int(kwargs["max_token_length"]) if kwargs["max_token_length"] else 8192
            )
        if "api_key" in kwargs:
            self.api_key = kwargs["api_key"]
        if "api_handles_prompt_template" in kwargs:
            self.api_handles_prompt_template = kwargs["api_handles_prompt_template"]

        log.warning("reconfigure", kwargs=kwargs)

        self.set_client(**kwargs)

    def jiggle_randomness(self, prompt_config: dict, offset: float = 0.3) -> dict:
        """
        adjusts temperature and repetition_penalty
        by random values using the base value as a center
        """

        temp = prompt_config["temperature"]
        
        if "frequency_penalty" in prompt_config:
            rep_pen_key = "frequency_penalty"
        else:
            rep_pen_key = "repetition_penalty"
        
        rep_pen = prompt_config[rep_pen_key]

        min_offset = offset * 0.3

        prompt_config["temperature"] = random.uniform(temp + min_offset, temp + offset)
        prompt_config[rep_pen_key] = random.uniform(
            rep_pen + min_offset * 0.3, rep_pen + offset * 0.3
        )