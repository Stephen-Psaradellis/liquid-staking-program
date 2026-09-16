"""EXP111 pass 2 — THE VALUE-MODEL HARNESS.

A faithful integer port of the value-moving arithmetic of the Marinade
liquid-staking program at commit b8fe3f8f9a2bb0978fb40ba5bb1c2855dd12940f.

Every formula cites the source line it reproduces. Widths and semantics:
  - all state fields are u64; intermediate products use u128 exactly where the
    Rust does (calc.rs / fee.rs).
  - `+`/`-`/`+=`/`-=` on u64 raise OverflowError, standing in for the Rust
    panic under `overflow-checks = true` (workspace Cargo.toml L6-7,
    programs/marinade-finance/Cargo.toml L18-19).
  - `.saturating_sub(...)` and floor `//` are used only where the code does.

Source root (read-only):
  .../marinade/programs/marinade-finance/src
File:line citations below are into that tree unless prefixed `agave:` (the
mirrored agave v2.3.0 stake_state.rs) or `pack` (the context pack).
"""

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1


def u64(x):
    """Coerce/assert a value is a valid u64; raise on out-of-range (a Rust
    store of an out-of-range value cannot happen — the arithmetic that
    produced it would have panicked first)."""
    if x < 0:
        raise OverflowError(f"u64 underflow: {x}")
    if x > U64_MAX:
        raise OverflowError(f"u64 overflow: {x}")
    return x


def add(a, b):
    """checked u64 add — Rust `a + b` / `a += b` under overflow-checks."""
    return u64(a + b)


def sub(a, b):
    """checked u64 sub — Rust `a - b` / `a -= b` under overflow-checks."""
    return u64(a - b)


def sat_sub(a, b):
    """u64 saturating_sub."""
    return a - b if a > b else 0


# ---------------------------------------------------------------------------
# calc.rs
# ---------------------------------------------------------------------------

def proportional(amount, numerator, denominator):
    """calc.rs:11-17  amount*numerator/denominator in u128, floor, u64::try_from."""
    if denominator == 0:
        return u64(amount)  # calc.rs:12-13
    val = (amount * numerator) // denominator  # calc.rs:15 (u128 mul, floor div)
    if val > U64_MAX:
        raise OverflowError("CalculationFailure")  # calc.rs:16 try_from err
    return val


def value_from_shares(shares, total_value, total_shares):
    """calc.rs:20-22 (alias for proportional)."""
    return proportional(shares, total_value, total_shares)


def shares_from_value(value, total_value, total_shares):
    """calc.rs:24-31."""
    if total_shares == 0:
        return u64(value)  # calc.rs:25-27 first mint 1:1
    return proportional(value, total_shares, total_value)  # calc.rs:29


# ---------------------------------------------------------------------------
# state/fee.rs
# ---------------------------------------------------------------------------

class Fee:
    """state/fee.rs:8-47  basis points, denominator 10_000."""
    MAX_BASIS_POINTS = 10_000

    def __init__(self, basis_points):
        self.basis_points = basis_points

    def apply(self, lamports):
        # fee.rs:43-46  u128 mul, floor div, cast back
        return u64((lamports * self.basis_points) // Fee.MAX_BASIS_POINTS)


class FeeCents:
    """state/fee.rs:74-113  bp_cents, denominator 1_000_000 (1e6 = 100%)."""
    MAX_BP_CENTS = 1_000_000

    def __init__(self, bp_cents):
        self.bp_cents = bp_cents

    def apply(self, lamports):
        # fee.rs:109-112
        return u64((lamports * self.bp_cents) // FeeCents.MAX_BP_CENTS)


# code caps (state/mod.rs:115-125, liq_pool.rs:34-36)
MAX_REWARD_FEE_BP = 1_000              # 10%  (state/mod.rs:115)
MAX_USER_FEE_BP_CENTS = 2_000          # 0.2% (state/mod.rs:121-125) — the four user fees
LP_MAX_FEE_BP = 1_000                  # 10%  (liq_pool.rs:34)
MAX_TREASURY_CUT_BP = 7_500            # 75%  (liq_pool.rs:36)
MIN_LIQUIDITY_TARGET = 50 * 1_000_000_000  # 50 SOL (liq_pool.rs:35)
LAMPORTS_PER_SOL = 1_000_000_000
MIN_STAKE_LOWER_LIMIT = LAMPORTS_PER_SOL // 100  # (state/mod.rs:128)


class InsufficientLiquidity(Exception):
    pass


class WithdrawAmountTooLow(Exception):
    pass


class ProgramError(Exception):
    pass


# ---------------------------------------------------------------------------
# The protocol + world (real on-chain balances) in one object.
# ---------------------------------------------------------------------------

class Marinade:
    """Holds State virtual counters (state/mod.rs) AND the real on-chain
    balances the CPIs move (reserve PDA, both liq-pool legs, the mSOL mint
    supply, the LP mint supply). Stake accounts are modelled abstractly."""

    def __init__(self, *,
                 total_active_balance=0,
                 delayed_unstake_cooling_down=0,
                 emergency_cooling_down=0,
                 available_reserve_balance=0,
                 msol_supply=0,
                 circulating_ticket_balance=0,
                 circulating_ticket_count=0,
                 rent_exempt_for_token_acc=2_039_280,   # rent for a token acct
                 min_deposit=1, min_withdraw=1,
                 min_stake=LAMPORTS_PER_SOL,             # 1 SOL default
                 staking_sol_cap=U64_MAX,
                 reward_fee_bp=0,
                 deposit_sol_fee_bpc=0,
                 deposit_stake_account_fee_bpc=0,
                 delayed_unstake_fee_bpc=0,
                 withdraw_stake_account_fee_bpc=0,
                 # liq pool
                 lp_liquidity_target=10_000 * LAMPORTS_PER_SOL,
                 lp_min_fee_bp=30, lp_max_fee_bp=300, treasury_cut_bp=2500,
                 lp_supply=0, liquidity_sol_cap=U64_MAX,
                 # real balances
                 reserve_real=None, sol_leg_real=None, msol_leg_real=0,
                 msol_mint_supply=None, lp_mint_supply=None,
                 treasury_valid=True,
                 last_stake_delta_epoch=0,
                 epoch=10):
        # --- State virtual counters (state/mod.rs:30-102) ---
        self.total_active_balance = total_active_balance
        self.delayed_unstake_cooling_down = delayed_unstake_cooling_down
        self.emergency_cooling_down = emergency_cooling_down
        self.available_reserve_balance = available_reserve_balance
        self.msol_supply = msol_supply
        self.circulating_ticket_balance = circulating_ticket_balance
        self.circulating_ticket_count = circulating_ticket_count
        self.rent = rent_exempt_for_token_acc
        self.min_deposit = min_deposit
        self.min_withdraw = min_withdraw
        self.min_stake = min_stake
        self.staking_sol_cap = staking_sol_cap
        self.reward_fee = Fee(reward_fee_bp)
        self.deposit_sol_fee = FeeCents(deposit_sol_fee_bpc)
        self.deposit_stake_account_fee = FeeCents(deposit_stake_account_fee_bpc)
        self.delayed_unstake_fee = FeeCents(delayed_unstake_fee_bpc)
        self.withdraw_stake_account_fee = FeeCents(withdraw_stake_account_fee_bpc)
        # liq pool (state/liq_pool.rs)
        self.lp_liquidity_target = lp_liquidity_target
        self.lp_min_fee = Fee(lp_min_fee_bp)
        self.lp_max_fee = Fee(lp_max_fee_bp)
        self.treasury_cut = Fee(treasury_cut_bp)
        self.lp_supply = lp_supply
        self.liquidity_sol_cap = liquidity_sol_cap
        self.last_stake_delta_epoch = last_stake_delta_epoch
        self.epoch = epoch
        self.treasury_valid = treasury_valid
        self.treasury_msol = 0  # real mSOL held by treasury (a token account)

        # --- real on-chain balances ---
        # reserve real = available_reserve_balance + rent (aligned at start)
        self.reserve_real = (reserve_real if reserve_real is not None
                             else available_reserve_balance + self.rent)
        self.sol_leg_real = (sol_leg_real if sol_leg_real is not None
                             else self.rent)  # empty pool still holds rent
        self.msol_leg_real = msol_leg_real
        self.msol_mint_supply = (msol_mint_supply if msol_mint_supply is not None
                                 else msol_supply)
        self.lp_mint_supply = (lp_mint_supply if lp_mint_supply is not None
                               else lp_supply)
        # stake accounts modelled abstractly, keyed by name
        self.stakes = {}   # name -> dict

    # ---- state/mod.rs value model ----
    def total_cooling_down(self):
        # state/mod.rs:208-210
        return add(self.delayed_unstake_cooling_down, self.emergency_cooling_down)

    def total_lamports_under_control(self):
        # state/mod.rs:213-217
        return add(add(self.total_active_balance, self.total_cooling_down()),
                   self.available_reserve_balance)

    def total_virtual_staked_lamports(self):
        # state/mod.rs:229-233
        return sat_sub(self.total_lamports_under_control(),
                       self.circulating_ticket_balance)

    def calc_msol_from_lamports(self, lamports):
        # state/mod.rs:236-242
        return shares_from_value(lamports, self.total_virtual_staked_lamports(),
                                 self.msol_supply)

    def msol_to_sol(self, msol_amount):
        # state/mod.rs:245-251
        return value_from_shares(msol_amount, self.total_virtual_staked_lamports(),
                                 self.msol_supply)

    def check_staking_cap(self, transfering_lamports):
        # state/mod.rs:219-227
        result = add(self.total_lamports_under_control(), transfering_lamports)
        if result > self.staking_sol_cap:
            raise ProgramError("StakingIsCapped")

    def check_liquidity_cap(self, transfering_lamports, sol_leg_balance):
        # liq_pool.rs:87-99
        result = add(sol_leg_balance, transfering_lamports)
        if result > self.liquidity_sol_cap:
            raise ProgramError("LiquidityIsCapped")

    def on_transfer_to_reserve(self, amount):
        self.available_reserve_balance = add(self.available_reserve_balance, amount)  # :277

    def on_transfer_from_reserve(self, amount):
        self.available_reserve_balance = sub(self.available_reserve_balance, amount)  # :281

    def on_msol_mint(self, amount):
        self.msol_supply = add(self.msol_supply, amount)  # :285

    def on_msol_burn(self, amount):
        self.msol_supply = sub(self.msol_supply, amount)  # :289

    def stake_delta(self, reserve_balance):
        # state/mod.rs:254-274
        raw = (sat_sub(reserve_balance, self.rent)
               + self.delayed_unstake_cooling_down
               - self.circulating_ticket_balance)
        if raw >= 0:
            return raw
        with_emergency = raw + self.emergency_cooling_down
        return min(with_emergency, 0)

    # ---- liq_pool.rs ----
    def lp_delta(self):
        return sat_sub(self.lp_max_fee.basis_points, self.lp_min_fee.basis_points)  # :60-63

    def linear_fee(self, lamports):
        # liq_pool.rs:67-77
        if lamports >= self.lp_liquidity_target:
            return self.lp_min_fee
        return Fee(self.lp_max_fee.basis_points
                   - proportional(self.lp_delta(), lamports, self.lp_liquidity_target))

    # =====================================================================
    # instructions/user/deposit.rs  (swap-first then mint)
    # =====================================================================
    def deposit(self, lamports, user):
        """deposit.rs:89-243. `user` is a dict with 'sol' and 'msol'."""
        if lamports < self.min_deposit:                     # :92-96
            raise ProgramError("DepositAmountIsTooLow")
        if user['sol'] < lamports:                          # :98-102
            raise ProgramError("NotEnoughUserFunds")
        if self.msol_mint_supply > self.msol_supply:        # :110-114
            raise ProgramError("UnregisteredMsolMinted")

        sol_fees = self.deposit_sol_fee.apply(lamports)     # :120
        lamports_minus_fee = sat_sub(lamports, sol_fees)    # :121
        user_msol_buy_order = self.calc_msol_from_lamports(lamports_minus_fee)  # :122

        msol_leg_balance = self.msol_leg_real               # :130
        msol_swapped = min(user_msol_buy_order, msol_leg_balance)  # :131

        if msol_swapped > 0:
            if user_msol_buy_order == msol_swapped:         # :137-139
                sol_swapped = lamports
            else:                                           # :140-143
                sol_swapped = self.msol_to_sol(msol_swapped)
            # transfer mSOL leg -> user (:148-163)
            self.msol_leg_real = sub(self.msol_leg_real, msol_swapped)
            user['msol'] = add(user['msol'], msol_swapped)
            # transfer lamports user -> sol leg (:166-175)
            user['sol'] = sub(user['sol'], sol_swapped)
            self.sol_leg_real = add(self.sol_leg_real, sol_swapped)
        else:
            sol_swapped = 0

        sol_deposited = sub(lamports, sol_swapped)          # :184
        if sol_deposited > 0:
            self.check_staking_cap(sol_deposited)           # :186
            user['sol'] = sub(user['sol'], sol_deposited)   # :189-198
            self.reserve_real = add(self.reserve_real, sol_deposited)
            self.on_transfer_to_reserve(sol_deposited)      # :199

        msol_minted = sub(user_msol_buy_order, msol_swapped)  # :203
        if msol_minted > 0:
            self.msol_mint_supply = add(self.msol_mint_supply, msol_minted)  # :206-221
            user['msol'] = add(user['msol'], msol_minted)
            self.on_msol_mint(msol_minted)                  # :222
        return {'sol_swapped': sol_swapped, 'sol_deposited': sol_deposited,
                'msol_swapped': msol_swapped, 'msol_minted': msol_minted,
                'msol_out': add(msol_swapped, msol_minted)}

    # =====================================================================
    # instructions/liq_pool/liquid_unstake.rs
    # =====================================================================
    def liquid_unstake(self, msol_amount, user):
        """liquid_unstake.rs:60-185."""
        if user['msol'] < msol_amount:                       # check_token_source_account
            raise ProgramError("NotEnoughUserFunds")
        liq_pool_available = sat_sub(self.sol_leg_real, self.rent)   # :77-78
        user_remove_lamports = self.msol_to_sol(msol_amount)         # :81
        if user_remove_lamports >= liq_pool_available:              # :82-88
            fee = self.lp_max_fee
        else:
            after = sub(liq_pool_available, user_remove_lamports)
            fee = self.linear_fee(after)
        msol_fee = fee.apply(msol_amount)                            # :91
        working = self.msol_to_sol(sub(msol_amount, msol_fee))      # :96
        if working + self.rent > self.sol_leg_real:                 # :99-103
            raise InsufficientLiquidity()
        if working < self.min_withdraw:                             # :105-109
            raise WithdrawAmountTooLow()
        # transfer SOL leg -> user (:112-128)
        if working > 0:
            self.sol_leg_real = sub(self.sol_leg_real, working)
            user['sol'] = add(user['sol'], working)
        treasury_cut = self.treasury_cut.apply(msol_fee) if self.treasury_valid else 0  # :131-135
        # transfer msol user -> msol leg  (msol_amount - treasury_cut)  (:139-149)
        to_leg = sub(msol_amount, treasury_cut)
        user['msol'] = sub(user['msol'], to_leg)
        self.msol_leg_real = add(self.msol_leg_real, to_leg)
        if treasury_cut > 0:                                        # :152-164
            user['msol'] = sub(user['msol'], treasury_cut)
            self.treasury_msol = add(self.treasury_msol, treasury_cut)
        # NOTE: msol_supply UNCHANGED — this is a transfer, not a burn.
        return {'working': working, 'msol_fee': msol_fee, 'treasury_cut': treasury_cut}

    # =====================================================================
    # instructions/liq_pool/add_liquidity.rs
    # =====================================================================
    def add_liquidity(self, lamports, user):
        """add_liquidity.rs:65-166."""
        if lamports < self.min_deposit:                              # :68-72
            raise ProgramError("DepositAmountIsTooLow")
        if lamports > user['sol']:                                   # :74-78
            raise ProgramError("NotEnoughUserFunds")
        self.check_liquidity_cap(lamports, self.sol_leg_real)        # :79-81
        if self.lp_mint_supply > self.lp_supply:                     # :86-90
            raise ProgramError("UnregisteredLPMinted")
        self.lp_supply = self.lp_mint_supply                         # :92 align down
        sol_leg_available = sub(self.sol_leg_real, self.rent)        # :103
        msol_leg_value = self.msol_to_sol(self.msol_leg_real)        # :104
        total_value = add(sol_leg_available, msol_leg_value)         # :105
        shares = shares_from_value(lamports, total_value, self.lp_supply)  # :114
        # transfer SOL user -> sol leg (:120-129)
        user['sol'] = sub(user['sol'], lamports)
        self.sol_leg_real = add(self.sol_leg_real, lamports)
        # mint LP (:133-149)
        self.lp_mint_supply = add(self.lp_mint_supply, shares)
        user['lp'] = add(user.get('lp', 0), shares)
        self.lp_supply = add(self.lp_supply, shares)                 # on_lp_mint :149
        return {'shares': shares}

    # =====================================================================
    # instructions/liq_pool/remove_liquidity.rs
    # =====================================================================
    def remove_liquidity(self, tokens, user):
        """remove_liquidity.rs:68-180."""
        if user.get('lp', 0) < tokens:
            raise ProgramError("NotEnoughUserFunds")
        lp_mint_supply = self.lp_mint_supply                         # :82
        if lp_mint_supply <= self.lp_supply:                         # :83-89
            self.lp_supply = lp_mint_supply
        sol_out = proportional(tokens, sub(self.sol_leg_real, self.rent), self.lp_supply)  # :92-96
        msol_out = proportional(tokens, self.msol_leg_real, self.lp_supply)                # :97-101
        if add(sol_out, self.msol_to_sol(msol_out)) < self.min_withdraw:  # :103-107
            raise WithdrawAmountTooLow()
        if sol_out > 0:                                              # :114-131
            self.sol_leg_real = sub(self.sol_leg_real, sol_out)
            user['sol'] = add(user['sol'], sol_out)
        if msol_out > 0:                                             # :133-151
            self.msol_leg_real = sub(self.msol_leg_real, msol_out)
            user['msol'] = add(user['msol'], msol_out)
        # burn LP (:153-164)
        self.lp_mint_supply = sub(self.lp_mint_supply, tokens)
        user['lp'] = sub(user['lp'], tokens)
        self.lp_supply = sub(self.lp_supply, tokens)                 # on_lp_burn :164
        return {'sol_out': sol_out, 'msol_out': msol_out}

    # =====================================================================
    # instructions/delayed_unstake/order_unstake.rs
    # =====================================================================
    def order_unstake(self, msol_amount, user):
        """order_unstake.rs:42-124. Returns a ticket dict."""
        if user['msol'] < msol_amount:
            raise ProgramError("NotEnoughUserFunds")
        sol_value = self.msol_to_sol(msol_amount)                    # :58
        fee = self.delayed_unstake_fee.apply(sol_value)              # :61-64
        lamports_for_user = sub(sol_value, fee)                      # :66
        if lamports_for_user < self.min_withdraw:                    # :68-72
            raise WithdrawAmountTooLow()
        self.circulating_ticket_balance = add(self.circulating_ticket_balance, lamports_for_user)  # :78
        self.circulating_ticket_count = add(self.circulating_ticket_count, 1)                       # :79
        # burn mSOL (:82-93)
        self.msol_mint_supply = sub(self.msol_mint_supply, msol_amount)
        user['msol'] = sub(user['msol'], msol_amount)
        self.on_msol_burn(msol_amount)
        created_epoch = self.epoch + (1 if self.epoch == self.last_stake_delta_epoch else 0)  # :96-101
        return {'lamports_amount': lamports_for_user, 'created_epoch': created_epoch}

    # =====================================================================
    # instructions/delayed_unstake/claim.rs
    # =====================================================================
    def claim(self, ticket, user):
        """claim.rs:50-152 (value body; WAIT_EPOCHS=1)."""
        if ticket['lamports_amount'] == 0:                           # :63-67
            raise ProgramError("ReusingDelayedUnstakeTicket")
        if self.epoch < ticket['created_epoch'] + 1:                 # :70-74 WAIT_EPOCHS
            raise ProgramError("TicketNotDue")
        lamports = ticket['lamports_amount']
        available = sub(self.reserve_real, self.rent)                # :101 (plain -, real)
        if lamports > available:                                     # :102-110
            raise ProgramError("TicketNotReady")
        self.circulating_ticket_balance = sub(self.circulating_ticket_balance, lamports)  # :116
        self.circulating_ticket_count = sub(self.circulating_ticket_count, 1)             # :117
        ticket['lamports_amount'] = 0                                # :119
        # transfer reserve -> user (:122-136)
        self.reserve_real = sub(self.reserve_real, lamports)
        user['sol'] = add(user['sol'], lamports)
        self.on_transfer_from_reserve(lamports)                      # :137
        return {'claimed': lamports}

    # =====================================================================
    # instructions/user/deposit_stake_account.rs
    # =====================================================================
    def deposit_stake_account(self, name, user):
        """deposit_stake_account.rs:75-324. Consumes an abstract stake account
        `name` the user controls (dict with delegation_stake, lamports, rent,
        activation_epoch, deactivation_epoch). Mints mSOL to the user."""
        s = self.stakes[name]
        if self.msol_mint_supply > self.msol_supply:                 # :79-83
            raise ProgramError("UnregisteredMsolMinted")
        if s['deactivation_epoch'] is not None:                      # :95-99 must be MAX
            raise ProgramError("RequiredActiveStake")
        if self.epoch < s['activation_epoch'] + 0:                   # :102-106 WAIT_EPOCHS=0
            raise ProgramError("DepositingNotActivatedStake")
        if s['delegation_stake'] < self.min_stake:                   # :109-113
            raise ProgramError("TooLowDelegationInDepositingStake")
        if s['lamports'] != s['delegation_stake'] + s['rent']:       # :118-122
            raise ProgramError("WrongStakeBalance")
        self.check_staking_cap(s['delegation_stake'])                # :124
        # validator.active_balance += stake (:142) then total below
        sol_fees = self.deposit_stake_account_fee.apply(s['delegation_stake'])  # :278
        deposit_minus_fee = sat_sub(s['delegation_stake'], sol_fees)            # :279
        msol_to_mint = self.calc_msol_from_lamports(deposit_minus_fee)          # :280-282
        # mint (:284-300)
        self.msol_mint_supply = add(self.msol_mint_supply, msol_to_mint)
        user['msol'] = add(user['msol'], msol_to_mint)
        self.on_msol_mint(msol_to_mint)
        self.total_active_balance = add(self.total_active_balance, s['delegation_stake'])  # :305
        # the stake account is now owned by the protocol (re-authorised, :217-267)
        s['owner'] = 'protocol'
        s['last_update_delegated_lamports'] = s['delegation_stake']
        s['last_update_status'] = 'Active'
        s['is_emergency_unstaking'] = False
        return {'msol_minted': msol_to_mint, 'sol_fees': sol_fees}

    # =====================================================================
    # instructions/user/withdraw_stake_account.rs
    # =====================================================================
    def withdraw_stake_account(self, src_name, msol_amount, user, out_name):
        """withdraw_stake_account.rs:104-375. Splits an active protocol stake
        `src_name` and hands the user a fresh active stake `out_name`."""
        s = self.stakes[src_name]
        if s.get('last_update_status') != 'Active':                  # :141-145
            raise ProgramError("RequiredActiveStake")
        if s.get('is_emergency_unstaking'):                          # :147-150
            raise ProgramError("StakeAccountIsEmergencyUnstaking")
        if s['deactivation_epoch'] is not None:                      # :156-160
            raise ProgramError("RequiredActiveStake")
        # check_stake_amount_and_validator: live delegation must equal record (:168-172)
        if s['delegation_stake'] != s['last_update_delegated_lamports']:
            raise ProgramError("StakeAccountNotUpdatedYet")
        if user['msol'] < msol_amount:
            raise ProgramError("NotEnoughUserFunds")
        sol_value = self.msol_to_sol(msol_amount)                    # :177
        if sol_value < self.min_withdraw:                            # :178-182
            raise WithdrawAmountTooLow()
        fee_lamports = self.withdraw_stake_account_fee.apply(sol_value)  # :185-186
        split_lamports = sub(sol_value, fee_lamports)                # :190
        if split_lamports < self.min_stake:                          # :194-198
            raise ProgramError("WithdrawStakeLamportsIsTooLow")
        if s['last_update_delegated_lamports'] < split_lamports:     # :200-204
            raise ProgramError("SelectedStakeAccountHasNotEnoughFunds")
        if sub(s['last_update_delegated_lamports'], split_lamports) < self.min_stake:  # :209-213
            raise ProgramError("StakeAccountRemainderTooLow")
        if self.treasury_valid:                                      # :219-224
            msol_fees = sat_sub(msol_amount, self.calc_msol_from_lamports(split_lamports))
        else:
            msol_fees = 0
        msol_burned = sub(msol_amount, msol_fees)                    # :225
        if msol_fees > 0:                                            # :227-238 transfer to treasury
            user['msol'] = sub(user['msol'], msol_fees)
            self.treasury_msol = add(self.treasury_msol, msol_fees)
        if msol_burned > 0:                                          # :241-253 burn
            self.msol_mint_supply = sub(self.msol_mint_supply, msol_burned)
            user['msol'] = sub(user['msol'], msol_burned)
            self.on_msol_burn(msol_burned)
        # split: source loses split_lamports of delegation; new acct gets it (:264-293)
        s['delegation_stake'] = sub(s['delegation_stake'], split_lamports)
        s['lamports'] = sub(s['lamports'], split_lamports)
        s['last_update_delegated_lamports'] = sub(s['last_update_delegated_lamports'], split_lamports)
        self.total_active_balance = sub(self.total_active_balance, split_lamports)  # :293
        # the split account inherits activation_epoch of the source (agave split)
        self.stakes[out_name] = {
            'delegation_stake': split_lamports,
            'lamports': split_lamports + s['rent'],   # dest funded with its own rent by payer
            'rent': s['rent'],
            'activation_epoch': s['activation_epoch'],
            'deactivation_epoch': None,
            'owner': 'user',
        }
        user['stakes'] = user.get('stakes', set()) | {out_name}
        return {'split_lamports': split_lamports, 'msol_burned': msol_burned,
                'msol_fees': msol_fees}

    # =====================================================================
    # instructions/crank/update.rs  — reward step of update_active
    # =====================================================================
    def update_active(self, name):
        """update.rs:307-465, the reward-booking body. Aligns virtual counters
        (begin), sweeps extra lamports, books delegation growth as rewards and
        mints the reward fee as mSOL to treasury."""
        s = self.stakes[name]
        # begin(): align reserve + msol supply (:147-172)
        self.available_reserve_balance = sat_sub(self.reserve_real, self.rent)
        self.msol_supply = self.msol_mint_supply
        is_treasury_ready = self.treasury_valid
        if s['deactivation_epoch'] is not None:                      # :332-336
            raise ProgramError("RequiredActiveStake")
        delegated = s['delegation_stake']                            # :347
        stake_balance_without_rent = sub(s['lamports'], s['rent'])   # :351
        extra = sat_sub(stake_balance_without_rent, delegated)       # :354
        if extra > 0:                                                # :356-365
            # withdraw extra to reserve
            s['lamports'] = sub(s['lamports'], extra)
            self.reserve_real = add(self.reserve_real, extra)
            self.on_transfer_to_reserve(extra)
            if is_treasury_ready:
                self._mint_protocol_fees(extra)
        last = s['last_update_delegated_lamports']
        if delegated >= last:                                        # :376-392
            rewards = sub(delegated, last)
            if is_treasury_ready:
                self._mint_protocol_fees(rewards)
            self.total_active_balance = add(self.total_active_balance, rewards)  # :390
        else:                                                        # :393-406 slashed
            slashed = sub(last, delegated)
            self.total_active_balance = sub(self.total_active_balance, slashed)
        s['last_update_delegated_lamports'] = delegated              # :412
        s['last_update_epoch'] = self.epoch
        # assert reserve aligned (:440-443)
        assert self.available_reserve_balance + self.rent == self.reserve_real
        return {}

    def _mint_protocol_fees(self, lamports_incoming):
        # update.rs:264-271
        fee = self.reward_fee.apply(lamports_incoming)
        fee_as_msol = self.calc_msol_from_lamports(fee)
        if fee_as_msol > 0:
            self.msol_mint_supply = add(self.msol_mint_supply, fee_as_msol)
            self.treasury_msol = add(self.treasury_msol, fee_as_msol)
            self.on_msol_mint(fee_as_msol)
        return fee_as_msol

    # =====================================================================
    # Abstract epoch reward step for stake accounts.
    # =====================================================================
    def add_stake(self, name, delegation_stake, activation_epoch, *,
                  rent=None, owner='user', deactivation_epoch=None):
        rent = rent if rent is not None else 2_282_880  # stake acct rent (frozen)
        self.stakes[name] = {
            'delegation_stake': delegation_stake,
            'lamports': delegation_stake + rent,
            'rent': rent,
            'activation_epoch': activation_epoch,
            'deactivation_epoch': deactivation_epoch,
            'owner': owner,
            'last_update_delegated_lamports': delegation_stake,
            'last_update_status': 'Active',
            'is_emergency_unstaking': False,
        }

    def epoch_boundary(self, reward_rate_num, reward_rate_den):
        """Advance one epoch. A stake earns rewards for the epoch just ending
        only if it was already effective (activation_epoch < current epoch).
        An activating stake (activation_epoch == current epoch) earns nothing
        (pack §4.1). Rewards land as growth in delegation.stake and account
        lamports (redelegated on-chain). fee = rate * effective stake, floored.
        """
        e = self.epoch
        for s in self.stakes.values():
            if s['deactivation_epoch'] is not None:
                continue  # deactivating/deactivated: no new rewards in this model
            if s['activation_epoch'] < e:  # already effective this epoch
                reward = (s['delegation_stake'] * reward_rate_num) // reward_rate_den
                s['delegation_stake'] = add(s['delegation_stake'], reward)
                s['lamports'] = add(s['lamports'], reward)
        self.epoch = e + 1

    # ---- value accounting for a party ----
    def price_num_den(self):
        """mSOL price = TVSL / msol_supply (as an exact rational)."""
        return self.total_virtual_staked_lamports(), self.msol_supply

    def party_value(self, user):
        """Total SOL-lamport value of a party: raw SOL + mSOL at post price +
        LP tokens at pool value + any stake accounts they own (delegation +
        rent, all withdrawable to them)."""
        val = user.get('sol', 0)
        tvsl, supply = self.price_num_den()
        m = user.get('msol', 0)
        if m:
            val += (m * tvsl) // supply if supply else 0
        lp = user.get('lp', 0)
        if lp and self.lp_supply:
            sol_leg_av = sat_sub(self.sol_leg_real, self.rent)
            msol_leg_val = (self.msol_leg_real * tvsl) // supply if supply else 0
            pool_val = sol_leg_av + msol_leg_val
            val += (lp * pool_val) // self.lp_supply
        for nm in user.get('stakes', set()):
            s = self.stakes[nm]
            val += s['lamports']  # user can withdraw the whole account
        return val
