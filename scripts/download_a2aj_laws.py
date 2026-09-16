"""Download every public A2AJ Canadian-laws parquet shard with resume support."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.ingest_open_legal_data import _download_parquet


LAW_CONFIGS = (
    "LEGISLATION-AB", "LEGISLATION-BC", "LEGISLATION-FED", "LEGISLATION-MB",
    "LEGISLATION-NB", "LEGISLATION-NL", "LEGISLATION-NS", "LEGISLATION-NT",
    "LEGISLATION-ON", "LEGISLATION-PE", "LEGISLATION-QC", "LEGISLATION-SK",
    "LEGISLATION-YT", "REGULATIONS-AB", "REGULATIONS-BC", "REGULATIONS-FED",
    "REGULATIONS-MB", "REGULATIONS-NB", "REGULATIONS-NL", "REGULATIONS-NS",
    "REGULATIONS-NT", "REGULATIONS-ON", "REGULATIONS-PE", "REGULATIONS-QC",
    "REGULATIONS-SK", "REGULATIONS-YT",
)


def main() -> int:
    cache_dir = Path("data/raw/a2aj")
    law_root = cache_dir / "a2aj_canadian-laws"
    failures: list[str] = []
    for position, config in enumerate(LAW_CONFIGS, start=1):
        target = law_root / config / "train.parquet"
        if target.exists() and target.stat().st_size > 0:
            print(f"[{position}/{len(LAW_CONFIGS)}] exists {config}", flush=True)
            continue
        try:
            print(f"[{position}/{len(LAW_CONFIGS)}] download {config}", flush=True)
            _download_parquet("a2aj/canadian-laws", config, cache_dir, max_download_mb=None)
            print(f"[{position}/{len(LAW_CONFIGS)}] done {config}", flush=True)
        except Exception as exc:
            failure = f"{config}: {exc}"
            failures.append(failure)
            print(f"[{position}/{len(LAW_CONFIGS)}] failed {failure}", flush=True)

    if failures:
        print("[RESULT]: FAILED", flush=True)
        return 1
    print("[RESULT]: SUCCESS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
