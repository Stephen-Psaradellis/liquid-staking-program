# Value-model harness for `marinade-finance/liquid-staking-program`

A faithful integer port of the value-moving arithmetic of the liquid-staking
program at commit `b8fe3f8f9a2bb0978fb40ba5bb1c2855dd12940f` (every formula in `marinade_model.py`
cites the source line it reproduces), plus a property search
(`run_property.py`) that runs random valid states through short sequences of
value-moving instructions and epoch steps and checks three invariants after
every instruction:

1. an attacker gains no value on a value path (epoch yield excepted);
2. no other party loses value except by a fee they paid;
3. solvency - no virtual counter exceeds its real backing.

## Measured

| run | sequences | instruction executions | violations |
|---|---|---|---|
| `python run_property.py 150000` | 150,000 | ~440,000 | 0 |

Sensitivity, so the zero is not vacuous: `python run_control.py 2000`
plants a +0.1% free-mSOL bug into `deposit` and the invariants flag it in
**359 of 2000** sequences.

The harness is a line-level derivation in Python, not `solana-program-test`;
it is meant to run in seconds in CI beside the Rust tests and to be extended
one instruction at a time. `python run_property.py 20000` is a few seconds.
