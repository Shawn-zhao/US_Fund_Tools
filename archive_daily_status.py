"""保留成功发布到 Pages 的原始日报，并查找此前实际发布的交易信号。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def archive_status(source: Path, archive_dir: Path, run_id: str, run_attempt: str) -> Path:
    if not (run_id.isascii() and run_id.isdecimal()
            and run_attempt.isascii() and run_attempt.isdecimal()):
        raise ValueError("GitHub Actions 运行编号与尝试编号必须为数字")
    payload = source.read_bytes()
    status = json.loads(payload)
    market_date = dt.date.fromisoformat(status["market_last"]).isoformat()
    signals = archive_dir / "signals"
    target = signals / market_date / f"{run_id}-{run_attempt}.json"
    for existing in signals.glob(f"????-??-??/{target.name}"):
        if existing == target and existing.read_bytes() == payload:
            return target
        raise ValueError(f"本次运行的日报已存档，且内容不同：{existing}")

    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as out:
        pending = Path(out.name)
    try:
        pending.write_bytes(payload)
        os.link(pending, target)
    except FileExistsError:
        if target.read_bytes() != payload:
            raise ValueError(f"本次运行的日报已存档，且内容不同：{target}") from None
    finally:
        pending.unlink()
    return target


def latest_published_signals(market_date, assets, archive_dir=None):
    """按交易日从新到旧查找；同日以最新一次数据齐全的发布为准。"""
    if archive_dir is None:
        configured = os.environ.get("MARKET_BREADTH_ARCHIVE_DIR")
        if not configured:
            return {}
        archive_dir = Path(configured)

    found = {}
    days = sorted((archive_dir / "signals").glob("????-??-??"), reverse=True)
    for day in days:
        if day.name > market_date:
            continue
        revisions = []
        for file in day.glob("*.json"):
            status = json.loads(file.read_bytes())
            if status.get("data_quality", {}).get("missing_current"):
                continue
            run, attempt = map(int, file.stem.split("-"))
            revisions.append((status, run, attempt))
        revisions.sort(key=lambda item: (item[0]["generated_epoch"], item[1], item[2]), reverse=True)
        for asset in assets:
            if asset in found:
                continue
            for status, _, _ in revisions:
                record = status["assets"].get(asset)
                if not record or record["decision_date"] != day.name:
                    continue
                signal = record["trade_signal"]
                if signal and signal["signal_date"] == day.name:
                    found[asset] = signal
                break
        if len(found) == len(assets):
            break
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("status", type=Path)
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("run_attempt")
    args = parser.parse_args()
    print(archive_status(args.status, args.archive_dir, args.run_id, args.run_attempt))
