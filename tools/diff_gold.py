"""Compare an exported area against the organisers' dev_gold, byte by byte.

Paths are built with pathlib, never with backslash literals: on Linux a backslash
is an ordinary filename character, so "out\\solar" would be one missing file
rather than a directory.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAG_KIT = ROOT / "_corpus" / "rag" / "RAG_ВИНИТИ_участникам_2026-11-20"

PAIRS = {
    "solar": (ROOT / "_corpus" / "area1" / "dev_gold", ROOT / "out" / "solar"),
    "rag": (RAG_KIT / "dev_gold", ROOT / "out" / "rag"),
}


def main(argv):
    area = argv[0] if argv else "solar"
    if area not in PAIRS:
        print("неизвестная область %s, доступны: %s" % (area, ", ".join(sorted(PAIRS))))
        return 2
    gold_dir, mine_dir = PAIRS[area]
    print("===", area)
    if not gold_dir.is_dir():
        print("нет эталона:", gold_dir)
        return 2
    if not mine_dir.is_dir():
        print("нет выгрузки:", mine_dir)
        return 2

    gold_files = {path.name for path in gold_dir.glob("*.csv")}
    mine_files = {path.name for path in mine_dir.glob("*.csv")}
    extra = sorted(mine_files - gold_files)
    absent = sorted(gold_files - mine_files)
    for name in absent:
        print("  %-20s MISSING  есть в эталоне, нет в выгрузке" % name)
    for name in extra:
        print("  %-20s EXTRA    есть в выгрузке, нет в эталоне" % name)

    identical = 0
    total = len(gold_files)
    for name in sorted(gold_files & mine_files):
        original = (gold_dir / name).read_bytes()
        produced = (mine_dir / name).read_bytes()
        if original == produced:
            identical += 1
            print("  %-20s IDENTICAL  %d bytes" % (name, len(original)))
            continue
        print("  %-20s DIFFERS    gold=%d mine=%d" % (name, len(original), len(produced)))
        left = original.split(b"\r\n")
        right = produced.split(b"\r\n")
        for index, (a, b) in enumerate(zip(left, right), 1):
            if a != b:
                print("     first difference at line", index)
                print("     gold:", a[:140])
                print("     mine:", b[:140])
                break
    print("%d of %d tables identical" % (identical, total))
    clean = identical == total and not extra and not absent
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
