from pathlib import Path
from category_policy import CategoryPolicy

root = Path(__file__).resolve().parent
p = CategoryPolicy(root)

print("allowed_count=", len(p.allowed_categories()))
print("valid=", p.normalize("game/minigames"))
print("legacy=", p.normalize("integration"))

try:
    p.normalize("general")
except ValueError as exc:
    print("reject_general=OK")
    print(str(exc).split("Allowed categories:", 1)[0].strip())
else:
    raise SystemExit("ERROR: general should have been rejected")

try:
    p.normalize("project/random-duplicate-name")
except ValueError:
    print("reject_unindexed=OK")
else:
    raise SystemExit("ERROR: unindexed category should have been rejected")

