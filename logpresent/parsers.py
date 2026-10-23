"""Optional summaries; the generic viewer does not depend on simulation formats."""

from datetime import datetime, timezone
import re

SNAPSHOT = re.compile(
    r"SNAPSHOT\s+(?P<snapshot>\S+)\s+state=(?P<state>\S+)\s+"
    r"files=(?P<done>\d+)\s*/\s*(?P<count>\d+)\s+"
    r"total=(?P<transferred>[\d.]+)\s*(?P<unit>\w+)\s*/\s*"
    r"(?P<total>[\d.]+)\s*(?P<total_unit>\w+)\s*"
    r"\((?P<percent>[\d.]+)%\).*?"
    r"speed=(?P<rate>[\d.]+\s*\S+).*?ETA=(?P<eta>\S+)"
)


def parse_transfer_status(rows: list[dict], file_id: str, mtime: float | None) -> dict:
    result = generic_summary(file_id, mtime)
    for row in reversed(rows):
        match = SNAPSHOT.search(row["text"])
        if not match:
            continue
        values = match.groupdict()
        result.update({
            "status": values["state"],
            "progress": min(1.0, max(0.0, float(values["percent"]) / 100)),
            "snapshot": values["snapshot"],
            "metrics": {
                "completed_files": f"{values['done']} / {values['count']}",
                "transferred": f"{values['transferred']} / {values['total']} {values['unit']}",
                "rate": values["rate"],
                "eta": values["eta"],
            },
        })
        timestamp = re.match(r"\[([^\]]+)\]", row["text"])
        if timestamp:
            try:
                result["updated_at"] = datetime.fromisoformat(timestamp[1]).isoformat()
            except ValueError:
                pass
        return result
    return result


def generic_summary(file_id: str, mtime: float | None) -> dict:
    return {
        "file_id": file_id,
        "status": "WAITING" if mtime is None else "AVAILABLE",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_log_update": mtime,
        "progress": None,
        "metrics": {},
        "snapshot": None,
    }
