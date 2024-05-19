"""Core LLM actions."""
# XXX(WARNING): Do not import __future__ annotations from typing
# This will cause class types to be resolved as strings

from collections.abc import Callable
from typing import Annotated, Any, Literal, Self, TypedDict

from pydantic import BaseModel, Field

from tracecat.experimental.registry import registry
from tracecat.llm import DEFAULT_MODEL_TYPE, ModelType, async_openai_call

TaskType = Literal[
    "llm.translate",
    "llm.extract",
    "llm.summarize",
    "llm.label",
    "llm.question_answering",
    "llm.choice",
]


class TaskFields(BaseModel):
    type: TaskType

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Self:
        task_type = data.pop("type")
        task_field_cls = TASK_FIELDS_FACTORY[task_type]
        return task_field_cls(**data)


class TranslateTaskFields(TaskFields):
    type: Literal["llm.translate"] = Field("llm.translate", frozen=True)
    from_language: str | None = Field(
        None,
        description="The source language for the translation. If None, the language(s) will be detected automatically.",
    )
    to_language: str = "english"


class ExtractTaskFields(TaskFields):
    type: Literal["llm.extract"] = Field("llm.extract", frozen=True)
    groups: list[str] | None = None


class LabelTaskFields(TaskFields):
    type: Literal["llm.label"] = Field("llm.label", frozen=True)
    labels: list[str]


class SummarizeTaskFields(TaskFields):
    type: Literal["llm.summarize"] = Field("llm.summarize", frozen=True)


class ChoiceTaskFields(TaskFields):
    type: Literal["llm.choice"] = Field("llm.choice", frozen=True)
    choices: list[str]


TaskFieldsVariant = (
    TranslateTaskFields
    | ExtractTaskFields
    | LabelTaskFields
    | SummarizeTaskFields
    | ChoiceTaskFields
)
TASK_FIELDS_FACTORY: dict[TaskType, type[TaskFields]] = {
    "llm.translate": TranslateTaskFields,
    "llm.extract": ExtractTaskFields,
    "llm.label": LabelTaskFields,
    "llm.summarize": SummarizeTaskFields,
    "llm.choice": ChoiceTaskFields,
}


def _event_context_instructions(action_trail: dict[str, Any]) -> str:
    return (
        "Additional Instructions:"
        "\nYou have also been provided with the following JSON object of the previous task execution results,"
        " delimited by triple backticks (```)."
        " The object keys are the action ids and the values are the results of the actions."
        "\n```"
        f"\n{action_trail}"
        "\n```"
        "You may use the past action run results to help you complete your task."
        " If you think it isn't helpful, you may ignore it."
    )


def _translate_system_context(from_language: str | None, to_language: str) -> str:
    # The corresponding `message` body for this should be a templated string.
    from_language = from_language or "Non-English"
    context = (
        "You are an expert translator. You will be provided with a body of text that may"
        f" contain {from_language.capitalize()} text, and your task is to translate it"
        f" into {to_language.capitalize()}."
        "\nFor example, given the following text:"
        "\nBonjour, comment ça va? My name is John. 你好吗?"
        "\nYou should respond with:"
        "\nHello, how are you? My name is John. How are you?"
    )
    return context


def _label_system_context(labels: list[str]) -> str:
    context = (
        "You will be provided with a body of text (e.g. paragraph, articles),"
        f" and your task is to classify it into one of the following categories: {labels}."
    )
    return context


def _extract_system_context(groups: list[str] | None) -> str:
    if groups:
        task = f"extract one or more lists of keywords from it, grouped by the following categories: {groups}"
    else:
        task = "extract a list of keywords from it"
    context = (
        "You will be provided with a body of text (e.g. paragraph, articles),"
        f" and your task is to {task}."
    )
    return context


def _summary_system_context() -> str:
    return (
        "You will be provided with a body of text (e.g. paragraph, articles),"
        " and your task is to summarize it."
    )


def _question_answering_system_context() -> str:
    return (
        "You are an expert at answering questions. Answer the following question in a concise and direct manner."
        " If you don't know the answer, don't make up an answer but say that you don't know the answer."
    )


def _choice_system_context(choices: list[str]) -> str:
    """Return the system context for a choice task.

    Example use cases
    -----------------
    1. Given a body of text containing some instructions, choose the best option from the following choices.
    2. Given an excerpt, choose the best option from the following choices.
    """
    return (
        "\nYou will be provided with a body of text from which you will use to make a decision."
        " Given a multiple choice question, choose the best option to answer the question."
        " Given a body of text containing some instructions, choose the best option to achieve the objective."
        f" Choose the best option from the following choices: {choices}"
    )


_LLM_SYSTEM_CONTEXT_FACTORY: dict[TaskType, Callable[..., str]] = {
    "llm.translate": _translate_system_context,
    "llm.extract": _extract_system_context,
    "llm.label": _label_system_context,
    "llm.summarize": _summary_system_context,
    "llm.question_answering": _question_answering_system_context,
    "llm.choice": _choice_system_context,
}


def _get_system_context(task_fields: TaskFields, event_context: dict[str, Any]) -> str:
    context = _LLM_SYSTEM_CONTEXT_FACTORY[task_fields.type](
        **task_fields.model_dump(exclude={"type"})
    )
    formatted_add_instrs = _event_context_instructions(event_context)
    return "\n".join((context, formatted_add_instrs))


def _generate_pydantic_json_response_schema(response_schema: dict[str, Any]) -> str:
    return (
        "\nCreate a `JSONDataResponse` according to the following pydantic model:"
        "\n```"
        "\nclass JSONDataResponse(BaseModel):"
        f"\n{"\n".join(f"\t{k}: {v}" for k, v in response_schema.items())}"
        "\n```"
    )


class LLMResponse(TypedDict):
    data: str | dict[str, Any]


@registry.register(
    namespace="core",
    version="0.1.0",
    description="Perform a LLM action",
)
async def call_llm(
    task_fields: Annotated[
        dict[str, Any],
        Field(description="Task fields"),
    ],
    message: Annotated[
        str,
        Field(description="Message to be sent"),
    ],
    system_context: Annotated[
        str,
        Field(description="Action to be taken"),
    ] = None,
    model: Annotated[
        ModelType,
        Field(description="Model to be used"),
    ] = DEFAULT_MODEL_TYPE,
    response_schema: Annotated[
        dict[str, Any],
        Field(description="Response schema"),
    ] = None,
    llm_kwargs: Annotated[
        dict[str, Any],
        Field(description="LLM kwargs"),
    ] = None,
    event_context: Annotated[
        dict[str, Any],
        Field(description="Event context"),
    ] = None,
) -> LLMResponse:
    llm_kwargs = llm_kwargs or {}
    event_context = event_context or {}

    # TODO(perf): Avoid re-creating the task fields object if possible
    validated_task_fields = TaskFields.from_dict(task_fields)

    if response_schema is None:
        system_context = _get_system_context(
            validated_task_fields,
            event_context=event_context,
        )
        text_response: str = await async_openai_call(
            prompt=message,
            model=model,
            system_context=system_context,
            response_format="text",
            **llm_kwargs,
        )
        return LLMResponse(data=text_response)
    else:
        system_context = "\n".join(
            (
                _get_system_context(validated_task_fields, event_context=event_context),
                _generate_pydantic_json_response_schema(response_schema),
            )
        )
        json_response: dict[str, Any] = await async_openai_call(
            prompt=message,
            model=model,
            system_context=system_context,
            response_format="json_object",
            **llm_kwargs,
        )
        if "JSONDataResponse" in json_response:
            inner_dict: dict[str, str] = json_response["JSONDataResponse"]
            return LLMResponse(data=inner_dict)
        return LLMResponse(data=json_response)
