from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "website/static/javascript/kiosk/queue.js"
s = SOURCE.read_text(encoding="utf-8")
assert "response.status >= 500" in s
assert "uncertain.queueOutcomeUnknown = true" in s
assert "printButton.disabled = Boolean(error.queueOutcomeUnknown)" in s
assert "await tryPrintQueue(" in s
assert "proceedToConfirmation(" in s
assert "data.queues" in s
print("Kiosk Batch B source regression: 6/6 passed")
