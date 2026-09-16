"""Negative control: plant a +0.1% free-mSOL bug into `deposit` and count
how many random sequences the invariants flag. A harness whose sensitivity is
not measured is a claim; this file is the measurement.

    python run_control.py [N]     # default 2000
"""
import sys
import marinade_model as mm
import run_property as rp

_orig = mm.Marinade.deposit


def planted_deposit(self, lamports, user):
    before = user["msol"]
    out = _orig(self, lamports, user)
    minted = user["msol"] - before
    extra = minted // 1000                      # +0.1% mSOL from nowhere
    if extra:
        user["msol"] = mm.add(user["msol"], extra)
        self.msol_supply = mm.add(self.msol_supply, extra)
        self.msol_mint_supply = mm.add(self.msol_mint_supply, extra)
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    mm.Marinade.deposit = planted_deposit
    flagged = sum(1 for seed in range(n) if rp.run_sequence(seed) is not None)
    print(f"planted +0.1% free mSOL in deposit: flagged {flagged} / {n} sequences")


if __name__ == "__main__":
    main()
