#!/usr/bin/env python3
"""Step reflection helpers."""

from __future__ import annotations

import os
from typing import Literal

from langchain_core.messages import SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from pydantic.v1 import BaseModel, Field

from meeseeks_core.classes import ActionStep
from meeseeks_core.common import format_action_argument, get_logger
from meeseeks_core.llm import build_chat_model

logging = get_logger(name="core.reflection")


class StepReflection(BaseModel):
    """Model output for step-level reflection."""

    status: Literal["ok", "retry", "revise"] = Field(default="ok")
    notes: str | None = None
    revised_argument: str | None = None


class StepReflector:
    """Reflect on tool results when objectives are provided."""

    def __init__(self, model_name: str | None) -> None:
        """Initialize the step reflector."""
        self._model_name = model_name

    def reflect(self, action_step: ActionStep, result_text: str) -> StepReflection | None:
        """Return a reflection decision for a step."""
        if not (
            action_step.objective or action_step.expected_output or action_step.execution_checklist
        ):
            return None
        if os.getenv("MEESEEKS_STEP_REFLECTION", "1") == "0":
            return None
        reflection_model = (
            os.getenv("STEP_REFLECTION_MODEL")
            or self._model_name
            or os.getenv("ACTION_PLAN_MODEL")
            or os.getenv("DEFAULT_MODEL")
        )
        if not reflection_model:
            return None
        parser = PydanticOutputParser(pydantic_object=StepReflection)  # type: ignore[type-var]
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(
                    content=(
                        "Reflect on whether the tool result satisfies the step objective. "
                        "Return status 'ok' if complete, 'retry' if the step should be "
                        "re-executed, or 'revise' if the action argument needs adjustment."
                    )
                ),
                HumanMessagePromptTemplate.from_template(
                    "Step title: {title}\n"
                    "Objective: {objective}\n"
                    "Checklist: {checklist}\n"
                    "Expected output: {expected}\n"
                    "Tool result: {result}\n\n"
                    "{format_instructions}"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["title", "objective", "checklist", "expected", "result"],
        )
        model = build_chat_model(
            model_name=reflection_model,
            temperature=0.0,
            openai_api_base=os.getenv("OPENAI_API_BASE"),
        )
        try:
            return (prompt | model | parser).invoke(
                {
                    "title": action_step.title or action_step.action_consumer,
                    "objective": action_step.objective
                    or format_action_argument(action_step.action_argument),
                    "checklist": "; ".join(action_step.execution_checklist or []),
                    "expected": action_step.expected_output or "Not specified",
                    "result": result_text,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("Step reflection failed: {}", exc)
            return None


__all__ = ["StepReflection", "StepReflector"]
