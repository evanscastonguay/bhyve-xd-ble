"""Offline tests for cli.py pure logic — the --device targeting parser.
(login/find are integration-tested live; here we test argument handling.)"""
import cli


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    return ok


def main():
    r = []
    print("cli.py offline tests:\n")

    # --device pulled out; int if numeric, str otherwise; rest preserved in order.
    r.append(check("no --device -> (None, args)", cli._extract_device(["start", "1", "300"]),
                   (None, ["start", "1", "300"])))
    r.append(check("--device name", cli._extract_device(["--device", "Back", "status"]),
                   ("Back", ["status"])))
    r.append(check("--device index (numeric -> int)", cli._extract_device(["status", "--device", "1"]),
                   (1, ["status"])))
    r.append(check("--device in the middle", cli._extract_device(["start", "--device", "Front", "1", "60"]),
                   ("Front", ["start", "1", "60"])))
    r.append(check("trailing --device with no value is ignored",
                   cli._extract_device(["status", "--device"]), (None, ["status"])))

    n = sum(r)
    print(f"\n{n}/{len(r)} cli checks passed" + ("  ✅" if n == len(r) else "  ❌"))
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
