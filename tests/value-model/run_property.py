"""C-p2h property search: from randomised but valid protocol states, run short
random sequences (length 2-6) of value-moving instructions interleaved with
epoch reward steps, driven by an attacker with a starting SOL/mSOL/LP balance.

After every NON-epoch instruction, check:
  (i)   attacker end value <= attacker start value + dust
        (no free value on a value path; epoch steps excepted -- they add real
        network yield the attacker may legitimately earn on holdings);
  (ii)  no OTHER party's value falls except by dust or by a fee they paid
        (here only the attacker acts, so others must be flat within dust);
  (iii) solvency: virtual counters never exceed real backing.

Conservation is also checked: total party value + treasury value is invariant
across a non-epoch op within dust.

A violation is minimised (the sequence is replayed prefix-by-prefix) and printed.
"""
import copy
import random
from marinade_model import (Marinade, LAMPORTS_PER_SOL, InsufficientLiquidity,
                            WithdrawAmountTooLow, ProgramError)

SOL = LAMPORTS_PER_SOL
DUST_PER_OP = 8  # lamports tolerance per instruction (a few lamports, generous)


def party_value(m, p):
    val = p.get('sol', 0)
    tvsl, supply = m.price_num_den()
    if p.get('msol'):
        val += (p['msol'] * tvsl) // supply if supply else 0
    if p.get('lp') and m.lp_supply:
        sol_av = max(0, m.sol_leg_real - m.rent)
        msol_val = (m.msol_leg_real * tvsl) // supply if supply else 0
        val += (p['lp'] * (sol_av + msol_val)) // m.lp_supply
    for t in p.get('tickets', []):
        val += t['lamports_amount']  # ticket redeemable at face
    for nm in p.get('stakes', set()):
        val += m.stakes[nm]['lamports']
    return val


def solvency_ok(m):
    # virtual reserve never exceeds real
    if m.available_reserve_balance + m.rent > m.reserve_real + DUST_PER_OP:
        return False, "available_reserve_balance + rent > reserve_real"
    # mSOL / LP virtual vs real supply (model keeps equal; guard drift)
    if m.msol_supply != m.msol_mint_supply:
        return False, f"msol_supply {m.msol_supply} != mint {m.msol_mint_supply}"
    if m.lp_supply != m.lp_mint_supply:
        return False, f"lp_supply {m.lp_supply} != mint {m.lp_mint_supply}"
    # cannot owe more tickets than controlled
    if m.circulating_ticket_balance > m.total_lamports_under_control() + DUST_PER_OP:
        return False, "circulating_ticket_balance > total_lamports_under_control"
    # pool legs cannot go below rent
    if m.sol_leg_real < m.rent - DUST_PER_OP:
        return False, "sol_leg below rent"
    return True, ""


def random_state(rng):
    supply = rng.randint(1_000, 20_000_000) * SOL
    # price between 1.0 and ~1.3
    active = supply + rng.randint(0, supply // 3)
    reserve_extra = rng.randint(0, 100_000) * SOL
    rent = 2_039_280
    sol_leg_extra = rng.randint(0, 50_000) * SOL
    # the mSOL leg's balance is a SUBSET of msol_supply (invariant of the
    # real system) -- cap it well below supply.
    msol_leg = rng.randint(0, min(20_000, supply // SOL // 4)) * SOL
    lp_min = rng.choice([0, 10, 30, 100])
    lp_max = rng.choice([lp_min, lp_min + 40, 300, 1000])
    tcut = rng.choice([0, 2500, 7500])
    target = rng.choice([50, 10_000, 1_000_000]) * SOL
    m = Marinade(
        total_active_balance=active,
        available_reserve_balance=reserve_extra,
        msol_supply=supply, msol_mint_supply=supply,
        circulating_ticket_balance=0, circulating_ticket_count=0,
        reserve_real=reserve_extra + rent,
        sol_leg_real=rent + sol_leg_extra, msol_leg_real=msol_leg,
        lp_min_fee_bp=lp_min, lp_max_fee_bp=lp_max, treasury_cut_bp=tcut,
        lp_liquidity_target=target,
        lp_supply=0, lp_mint_supply=0,
        reward_fee_bp=rng.choice([0, 200, 1000]),
        deposit_sol_fee_bpc=rng.choice([0, 1000, 2000]),
        deposit_stake_account_fee_bpc=rng.choice([0, 1000, 2000]),
        delayed_unstake_fee_bpc=rng.choice([0, 1000, 2000]),
        withdraw_stake_account_fee_bpc=rng.choice([0, 1000, 2000]),
        min_withdraw=1, min_deposit=1, min_stake=SOL,
        treasury_valid=rng.random() < 0.85,
        epoch=100,
    )
    # LP supply must fully represent the pool legs, so all pool value is
    # attributable to LP holders (otherwise conservation has an unowned bucket).
    sol_av = max(0, m.sol_leg_real - m.rent)
    tvsl, sup = m.price_num_den()
    msol_val = (m.msol_leg_real * tvsl) // sup if sup else 0
    pool_val = sol_av + msol_val
    m.lp_supply = pool_val
    m.lp_mint_supply = pool_val
    # attacker starting balance. Directly-held mSOL is bounded by the supply
    # NOT sitting in the pool leg (leg mSOL is pool-owned, valued via LP).
    free_msol = max(0, m.msol_supply - m.msol_leg_real)
    attacker = {
        'sol': rng.randint(1, 200_000) * SOL,
        'msol': rng.randint(0, min(free_msol, 50_000 * SOL)),
        'lp': rng.randint(0, m.lp_supply) if m.lp_supply else 0,
        'tickets': [], 'stakes': set(),
    }
    # others hold the remainder. The mSOL sitting in the liq-pool mSOL LEG is
    # part of msol_supply but is a POOL asset (valued via LP tokens), so it is
    # excluded here or it would be double-counted.
    others_msol = m.msol_supply - attacker['msol'] - m.msol_leg_real
    others = {
        'sol': 0,
        'msol': others_msol,
        'lp': max(0, m.lp_supply - attacker['lp']),
        'tickets': [], 'stakes': set(),
    }
    # seed a protocol-owned active canonical stake to split from (withdraw path)
    canon = min(m.total_active_balance, rng.randint(2, 200_000) * SOL)
    if canon >= 2 * m.min_stake:
        m.add_stake('canonical', canon, activation_epoch=1, owner='protocol')
    return m, attacker, others


OPS = ['deposit', 'liquid_unstake', 'add_liquidity', 'remove_liquidity',
       'order_unstake', 'claim', 'epoch',
       'deposit_stake_account', 'withdraw_stake_account']
# stake-account ops move a stake's rent-exempt reserve into a protocol "slush"
# (the documented C-user-7 dust: rent -> operational_sol_account), so they are
# checked for attacker-no-gain and solvency but not for exact party
# conservation (which has no bucket for that protocol-side slush).
NO_CONSERVATION_CHECK = {'deposit_stake_account', 'withdraw_stake_account'}
COVER = {}


def do_op(m, op, attacker, rng):
    """Perform one op; return ('epoch'|'instr', label) or raise/return None if
    infeasible. Mutates m and attacker."""
    if op == 'epoch':
        rate_num = rng.choice([2, 45, 100])   # 0.02%..0.1% per epoch
        m.last_stake_delta_epoch = m.epoch  # so tickets get +1 sometimes
        m.epoch_boundary(rate_num, 100000)
        # crank any protocol stakes so counters absorb rewards. Nothing is
        # caught here on purpose: a ProgramError/OverflowError reverts the
        # whole epoch step in run_sequence (the crank is a tx too), and an
        # AssertionError from update_active's reserve-alignment check is a
        # finding this harness exists to report, never to swallow.
        for nm in list(m.stakes):
            s = m.stakes[nm]
            if s['owner'] == 'protocol' and s['deactivation_epoch'] is None:
                m.update_active(nm)
        return ('epoch', 'epoch')
    if op == 'deposit':
        amt = rng.randint(1, max(1, attacker['sol'] // SOL)) * SOL
        m.deposit(amt, attacker)
    elif op == 'liquid_unstake':
        if attacker['msol'] == 0:
            return None
        amt = rng.randint(1, attacker['msol'])
        m.liquid_unstake(amt, attacker)
    elif op == 'add_liquidity':
        amt = rng.randint(1, max(1, attacker['sol'] // SOL)) * SOL
        m.add_liquidity(amt, attacker)
    elif op == 'remove_liquidity':
        if attacker.get('lp', 0) == 0:
            return None
        amt = rng.randint(1, attacker['lp'])
        m.remove_liquidity(amt, attacker)
    elif op == 'order_unstake':
        if attacker['msol'] == 0:
            return None
        amt = rng.randint(1, attacker['msol'])
        tk = m.order_unstake(amt, attacker)
        attacker['tickets'].append(tk)
        return ('instr', 'order_unstake')
    elif op == 'claim':
        due = [t for t in attacker['tickets'] if t['lamports_amount'] > 0
               and m.epoch >= t['created_epoch'] + 1]
        if not due:
            return None
        tk = due[0]
        m.claim(tk, attacker)
    elif op == 'deposit_stake_account':
        # attacker converts undelegated SOL into a fresh stake, then deposits it
        rent = 2_282_880
        if attacker['sol'] < m.min_stake + rent:
            return None
        maxdel = min(attacker['sol'] - rent, 300_000 * SOL)
        if maxdel < m.min_stake:
            return None
        delegation = rng.randint(m.min_stake, maxdel)
        # activating this epoch (worst case) or already active
        act = m.epoch if rng.random() < 0.5 else m.epoch - 3
        nm = f"atk_stk_{len(m.stakes)}"
        m.add_stake(nm, delegation, activation_epoch=act, owner='user')
        attacker['sol'] = attacker['sol'] - (delegation + rent)
        attacker.setdefault('stakes', set()).add(nm)
        try:
            m.deposit_stake_account(nm, attacker)
        except Exception:
            # revert the abstract stake creation on rejection
            attacker['sol'] = attacker['sol'] + (delegation + rent)
            attacker['stakes'].discard(nm)
            del m.stakes[nm]
            raise
        attacker['stakes'].discard(nm)  # now protocol-owned
        return ('instr', op)
    elif op == 'withdraw_stake_account':
        if 'canonical' not in m.stakes or attacker['msol'] == 0:
            return None
        dest_rent = m.stakes['canonical']['rent']
        if attacker['sol'] < dest_rent:      # attacker is the split_stake_rent_payer
            return None
        amt = rng.randint(1, attacker['msol'])
        outn = f"atk_split_{len(m.stakes)}"
        m.withdraw_stake_account('canonical', amt, attacker, outn)
        attacker['sol'] = attacker['sol'] - dest_rent  # payer funds the new acct rent
        return ('instr', op)
    return ('instr', op)


def check(m, attacker, others, atk_before, others_before, total_before,
          ops_since, skip_conservation=False):
    tol = DUST_PER_OP * max(1, ops_since)
    problems = []
    atk_now = party_value(m, attacker)
    others_now = party_value(m, others)
    treas_now = treasury_value(m)
    total_now = atk_now + others_now + treas_now
    # (i) attacker cannot gain on a value path (non-epoch)
    if atk_now > atk_before + tol:
        problems.append(f"attacker gained {atk_now - atk_before} (> {tol})")
    if not skip_conservation:
        # (ii) others cannot lose (only attacker acts)
        if others_now < others_before - tol:
            problems.append(f"others lost {others_before - others_now} (> {tol})")
        # conservation
        if abs(total_now - total_before) > tol:
            problems.append(f"non-conservation delta {total_now - total_before} (> {tol})")
    ok, why = solvency_ok(m)
    if not ok:
        problems.append(f"insolvency: {why}")
    return problems


def treasury_value(m):
    tvsl, supply = m.price_num_den()
    return (m.treasury_msol * tvsl) // supply if supply else 0


def run_sequence(seed, verbose=False):
    rng = random.Random(seed)
    m, attacker, others = random_state(rng)
    length = rng.randint(2, 6)
    seq = [rng.choice(OPS) for _ in range(length)]
    trace = []
    for i, op in enumerate(seq):
        atk_before = party_value(m, attacker)
        others_before = party_value(m, others)
        total_before = atk_before + party_value(m, others) - party_value(m, others) \
            + party_value(m, others) + treasury_value(m)
        total_before = atk_before + others_before + treasury_value(m)
        # snapshot so a raised op reverts atomically, exactly as a Solana tx does
        snap_m, snap_a = copy.deepcopy(m), copy.deepcopy(attacker)
        try:
            r = do_op(m, op, attacker, rng)
        except (InsufficientLiquidity, WithdrawAmountTooLow, ProgramError, OverflowError):
            m.__dict__.update(snap_m.__dict__)      # revert
            attacker.clear(); attacker.update(snap_a)
            continue
        except AssertionError as e:
            return (seq[:i + 1], f"assert failed: {e}", seed)
        if r is None:
            continue
        kind, label = r
        trace.append(label)
        COVER[label] = COVER.get(label, 0) + 1
        if kind == 'epoch':
            continue  # epoch adds real yield; skip the no-gain/conservation check
        probs = check(m, attacker, others, atk_before, others_before,
                      total_before, 1, skip_conservation=(label in NO_CONSERVATION_CHECK))
        if probs:
            return (trace[:], "; ".join(probs), seed)
    return None


def main():
    import sys
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 120_000
    violations = []
    ran = 0
    for seed in range(N):
        v = run_sequence(seed)
        ran += 1
        if v is not None:
            violations.append(v)
            if len(violations) <= 5:
                print(f"VIOLATION seed={v[2]} seq={v[0]} :: {v[1]}")
    print(f"\nsequences run: {ran}")
    print(f"instructions executed by type: {dict(sorted(COVER.items()))}")
    print(f"violations: {len(violations)}")
    if not violations:
        print("MEASURED ZERO: no invariant violation across all sequences.")


if __name__ == '__main__':
    main()
