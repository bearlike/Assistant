from pydantic import BaseModel, ConfigDict, Field
from truss_core.common import pydantic_to_openai_tool


class _SampleArgs(BaseModel):
    """Sample description."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The name.")
    count: int = Field(default=0, description="Item count.")


def test_schema_shape_matches_existing_internal_schemas():
    schema = pydantic_to_openai_tool(_SampleArgs, name="do_thing")
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "do_thing"
    assert fn["description"].startswith("Sample description")
    params = fn["parameters"]
    assert params["type"] == "object"
    props = params["properties"]
    assert props["name"]["type"] == "string"
    assert props["name"]["description"] == "The name."
    assert "title" not in params
    assert "title" not in props["name"]
    assert params["required"] == ["name"]


def test_respects_extra_forbid():
    """additionalProperties: false should survive from ConfigDict(extra='forbid')."""
    schema = pydantic_to_openai_tool(_SampleArgs, name="do_thing")
    assert schema["function"]["parameters"].get("additionalProperties") is False


def test_rejects_non_pydantic():
    import pytest
    with pytest.raises(TypeError):
        pydantic_to_openai_tool(dict, name="bogus")
