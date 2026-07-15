from __future__ import annotations

from henry.sandbox_types import CellOutput, CellResult


def test_cell_result_defaults() -> None:
    result = CellResult()
    assert result.status == "ok"
    assert result.outputs == []
    assert result.execution_count == 0
    assert result.timed_out is False
    assert result.truncated is False


def test_cell_output_preserves_order_and_mime_bundle() -> None:
    outputs = [
        CellOutput(output_type="stream", name="stdout", text="before\n"),
        CellOutput(output_type="display_data", data={"image/png": "b64", "text/plain": "<Figure>"}),
        CellOutput(output_type="stream", name="stdout", text="after\n"),
        CellOutput(output_type="execute_result", data={"text/plain": "42"}, execution_count=3),
    ]
    result = CellResult(status="ok", outputs=outputs, execution_count=3)
    assert [output.output_type for output in result.outputs] == [
        "stream",
        "display_data",
        "stream",
        "execute_result",
    ]
    assert result.outputs[1].data["image/png"] == "b64"
    assert result.outputs[1].data["text/plain"] == "<Figure>"
