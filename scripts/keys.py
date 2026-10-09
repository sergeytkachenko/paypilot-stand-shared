import argparse
import csv
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import budget, workspace  # noqa: E402


def cmd_create(args) -> int:
    labels = list(args.label or [])
    if args.labels_file:
        labels += [line.strip() for line in Path(args.labels_file).read_text(
            encoding="utf-8").splitlines() if line.strip()]
    if args.count:
        labels += [f"student-{i:02d}" for i in range(1, args.count + 1)]
    if not labels:
        print("Назвіть ключі: --label, --labels-file або --count", file=sys.stderr)
        return 2
    out = csv.writer(sys.stdout)
    out.writerow(["label", "key", "daily_usd", "monthly_usd"])
    for label in labels:
        key, raw = budget.create(label, args.daily, args.monthly)
        out.writerow([key.label, raw, key.daily_usd, key.monthly_usd])
    print(f"Створено ключів: {len(labels)}. Повний ключ показано лише зараз.",
          file=sys.stderr)
    return 0


def cmd_list(args) -> int:
    out = csv.writer(sys.stdout)
    out.writerow(["prefix", "label", "revoked", "spent_24h_usd", "daily_usd",
                  "spent_30d_usd", "monthly_usd", "has_db", "settings"])
    for key in budget.all_keys():
        u = budget.usage(key)
        out.writerow([key.prefix, key.label, int(key.revoked),
                      u["day"]["spent_usd"], key.daily_usd,
                      u["month"]["spent_usd"], key.monthly_usd,
                      int(workspace.db_path(key).exists()),
                      json.dumps(workspace.settings(key).as_dict(), ensure_ascii=False)])
    return 0


def cmd_revoke(args) -> int:
    key = budget.find(args.prefix)
    budget.revoke(key)
    dropped = workspace.drop(key)
    print(f"Відкликано {key.prefix} ({key.label})"
          + ("; базу ключа видалено" if dropped else ""))
    return 0


def cmd_set_limits(args) -> int:
    key = budget.set_limits(budget.find(args.prefix), args.daily, args.monthly)
    print(f"{key.prefix} ({key.label}): ${key.daily_usd:g}/24 год, "
          f"${key.monthly_usd:g}/30 днів")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ключі студентів спільного стенду")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="видати ключі; друкує CSV label,key")
    create.add_argument("--label", action="append", help="ім'я студента; можна кілька разів")
    create.add_argument("--labels-file", help="файл з іменами, по одному на рядок")
    create.add_argument("--count", type=int, help="N ключів student-01..student-N")
    create.add_argument("--daily", type=float, help="ліміт за 24 год, $")
    create.add_argument("--monthly", type=float, help="ліміт за 30 днів, $")
    create.set_defaults(fn=cmd_create)

    sub.add_parser("list", help="ключі й витрати").set_defaults(fn=cmd_list)

    revoke = sub.add_parser("revoke", help="відкликати ключ за префіксом")
    revoke.add_argument("prefix")
    revoke.set_defaults(fn=cmd_revoke)

    limits = sub.add_parser("set-limits", help="змінити ліміти ключа")
    limits.add_argument("prefix")
    limits.add_argument("--daily", type=float)
    limits.add_argument("--monthly", type=float)
    limits.set_defaults(fn=cmd_set_limits)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
