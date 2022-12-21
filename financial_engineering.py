from enum import IntEnum
from time import time
from typing import Dict, List, Tuple

from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.implied_volatility import implied_volatility


##################
# TYPE DEFINITIONS
##################
class MarginType(IntEnum):
    INITIAL = 1
    MAINTENANCE = 2


class Instrument(IntEnum):
    PERPETUAL = 1
    FUTURE = 2
    OPTION_CALL = 3
    OPTION_PUT = 4


class Asset(IntEnum):
    USDC = 1
    ETH = 2
    BTC = 3
    OPTION_PUT = 4


def encode_a(a: Asset) -> str:
    return str(a)


def decode_a(asset_id: str) -> Asset:
    return Asset(asset_id)


class Derivative:
    def __init__(self, instrument: Instrument, underlying_asset: Asset, resolution: int, expiration_date: int, strike_price: int):
        self.instrument = instrument  # int2
        self.underlying_asset = underlying_asset  # int16
        self.resolution = resolution  # int4
        # Only for options, and futures
        # int24: unix days (always expires at 8am UTC)
        self.expiration_date = expiration_date
        # int32: Only for options
        self.strike_price = strike_price


def encode_d(d: Derivative) -> str:
    """Demo Only: This will be replaced to a more efficient asset_id encoding in prod"""
    parts = [
        d.instrument.__str__(),
        d.underlying_asset.__str__(),
        d.resolution.__str__(),
        d.expiration_date.__str__(),
        d.strike_price.__str__(),
    ]
    return ":".join(parts)


def decode_d(asset_id: str) -> Derivative:
    """Demo Only: This will be replaced to a more efficient asset_id encoding in prod"""
    parts = asset_id.split(":")
    return Derivative(
        Instrument(parts[0]),
        Asset(parts[1]),
        int(parts[2]),
        int(parts[3]),
        int(parts[4]),
    )


class Position:
    """
    balance is resolutionised as ints in StarkEx
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/position/position.cairo#L8

    This is currently called PositionAsset, but traders usually refer to each deriv long/short as a position.
    We recommend calling this Position.
    """

    def __init__(self, asset_id: str, balance: float, cached_funding_index: float):
        # the string encoded form of a derivative
        self.asset_id = asset_id
        # Amount of position held
        self.balance = balance
        # This is the cached funding index for perpetuals
        self.cached_funding_index = cached_funding_index


class Vault:
    """
    collateral_balance is quantized as ints in StarkEx
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/position/position.cairo#L16

    This is currently called Position in code, but Vault in documentation. 
    Vault is the more intuitive term that we recommend. Position usually refers to a long/short.
    """

    def __init__(self, public_key: str, collateral: Asset, collateral_balance: float, positions: List[Position]):
        self.public_key = public_key
        # the collateral asset
        self.collateral = collateral
        # the balance of collateral in the vault
        self.collateral_balance = collateral_balance
        # list of positions
        self.positions = positions


#############
# SAMPLE DATA
#############
"""
The sample data is used for demo purposes only to help make sense of the code.

NOW is used as system clock time.
"""
NOW = round(time())
UNIX_DAYS_NOW = NOW // 86400

SAMPLE_CALL = Derivative(
    Instrument.OPTION_CALL,
    Asset.ETH, 4, UNIX_DAYS_NOW + 10, 1200
)
SAMPLE_PUT = Derivative(
    Instrument.OPTION_PUT,
    Asset.ETH, 4, UNIX_DAYS_NOW + 15, 1100
)
SAMPLE_FUTURE = Derivative(
    Instrument.FUTURE,
    Asset.ETH, 4, UNIX_DAYS_NOW + 30, 0
)
SAMPLE_PERP = Derivative(
    Instrument.PERPETUAL,
    Asset.ETH, 4,  0, 0
)

SAMPLE_EXPIRED_OPTION = Derivative(
    Instrument.OPTION_CALL,
    Asset.ETH, 4, UNIX_DAYS_NOW - 1, 1200
)

SAMPLE_VAULT = Vault("0xdeadbeef", Asset.USDC, 1000000, [
    Position(encode_d(SAMPLE_CALL), 1000, 0),
    Position(encode_d(SAMPLE_PUT), 400, 0),
    Position(encode_d(SAMPLE_FUTURE), -100, 0),
    Position(encode_d(SAMPLE_PERP), -500, 1181.12),
    Position(encode_d(SAMPLE_EXPIRED_OPTION), -200, 0)
]
)


######################################
# PRICE STATE (price feed via oracles)
######################################
"""
Index prices keep track of spot index prices across exchanges/defi.
This price source is the most stable given its distributed nature.
It is also more trustless.

We may assume that this is always available for spot. Oracles refreshes this.
"""
ORACLE_INDEX_PRICES: Dict[str, float] = {
    encode_a(Asset.USDC): 1.000,
    encode_a(Asset.ETH): 1182.42,
    encode_a(Asset.BTC): 16739.50
}
"""
Market prices are uploaded by the operator based on best bid/ask pair.
This is required for options where spot index prices are not useful for deriving value.
But this also helps provide futures/perps market prices to make margin
computations more accurate.

The drawback of this pricing source is that it requires some trust.
If this pricing source submits the best bid and best ask signatures,
its trustlessness improves substantially.

We may assume that this is always available for options. Operator refreshes this.
"""
MARKET_PRICES: Dict[str, float] = {
    encode_a(Asset.USDC): 1.000,
    encode_a(Asset.ETH): 1182.42,
    encode_d(SAMPLE_CALL): 848.23,
    encode_d(SAMPLE_PUT): 839.12,
    encode_d(SAMPLE_PERP): 1182.12
}
"""
Moving averages are backward looking market prices. 
It is far more trustless than market prices since they use transacted
orders which are forge-proof.

The drawback of this pricing source is that its accuracy falls off
substantially in non-liquid markets. Using a time-bound filter fixes
the accuracy problem, but makes this data source less available.

There are many ways we can compute moving averages.
1. Average (or EWMA) of last N orders
2. Average (or EWMA) of orders from last N minutes
3. Combination of above two

We do not assume that this is always available.
"""
MOVING_AVERAGE_PRICES: Dict[str, float] = {
    encode_a(Asset.ETH): 1182.42,
    encode_a(Asset.BTC): 16739.50,
    encode_d(SAMPLE_PUT): 839.16,
    encode_d(SAMPLE_FUTURE): 1182.19
}

"""
The price at which all expired options and futures settled.

There is one entry here per underlying asset, and expiry date.
"""
SETTLEMENT_PRICES: Dict[str, float] = {
    # The key is an encoded derivative with just the asset and expiry filled
    # eg. It specifies, on 24 Dec 2022 0800 UTC, ETH was at 1182.66
    str(0x003000232000000): 1182.66,
}

# This is a bit mask that applies to the derivative to figure out whether it
# has been settled
#
# eg.
# settled_price = SETTLEMENT_PRICES.get(asset_id & SETTLEMENT_MASK, None)
# if settled_price == None:
#     "not expired"
# else:
#     "expired at settled_price"
SETTLEMENT_MASK: str = str(0x004000fff000000)


class Prices:
    def __init__(self, oracle_index: Dict[str, float], market: Dict[str, float], moving_avg: Dict[str, float], settled: Dict[str, float]):
        self.oracle_index = oracle_index
        self.market = market
        self.moving_avg = moving_avg
        self.settled = settled


PRICES = Prices(ORACLE_INDEX_PRICES, MARKET_PRICES,
                MOVING_AVERAGE_PRICES, SETTLEMENT_PRICES)

###################################
# STARKEX CONFIGS (set by operator)
###################################
PORTFOLIO_MARGIN_INITIAL_MULTIPLIER = 1.3


class AssetConfig:
    def __init__(self, fvm: int, fim: float, fmm:  float, oim:  float, omm:  float, srs: float, vrs: float, rfr: float):
        self.future_variable_margin = fvm
        self.future_initial_margin = fim
        self.future_maintenance_margin = fmm
        self.option_initial_margin = oim
        self.option_maintenance_margin = omm
        self.spot_range_simulation = srs
        self.vol_range_simulation = vrs
        self.risk_free_rate = rfr


ASSET_CONFIG: Dict[Asset, AssetConfig] = {
    Asset.ETH: AssetConfig(50000, 0.01, 0.02, 0.05, 0.10, 0.2, 0.45, 0.0),
    Asset.BTC: AssetConfig(50000, 0.01, 0.02, 0.05, 0.10, 0.2, 0.45, 0.0)
}

########################
# MARK PRICE COMPUTATION
########################


def get_spot_mark_price(asset_id: str, prices: Prices) -> float:
    """
    Spot mark price is the average of all three price sources (whenever available)
    """
    sources = [prices.oracle_index[asset_id]]
    market = prices.market.get(asset_id, None)
    if market != None:
        sources.append(market)
    moving_avg = prices.moving_avg.get(asset_id, None)
    if moving_avg != None:
        sources.append(moving_avg)
    return sum(sources) / len(sources)


def get_deriv_settled_price(asset_id: str, prices: Prices):
    """
    Determines price if derivative (options & futures) has already been settled
    """
    settled_price = prices.settled.get(
        str(int(asset_id) & int(SETTLEMENT_MASK)), None)
    if settled_price == None:
        return None
    deriv = decode_d(asset_id)
    if deriv.instrument == Instrument.FUTURE:
        return settled_price
    elif deriv.instrument == Instrument.OPTION_CALL or deriv.instrument == Instrument.OPTION_PUT:
        # In the money options expire with a value, out of the money options expire worthless
        return max(0, settled_price - deriv.strike_price)
    return None  # will never be hit


def get_deriv_mark_price(asset_id: str, prices: Prices) -> float:
    """
    Mark Price is the simple average of all three price sources (whenever available)
    If the derivative is already settled, it uses the settled price instead
    """
    settled_price = get_deriv_settled_price(asset_id, prices)
    if settled_price != None:
        return settled_price
    deriv = decode_d(asset_id)
    sources = []
    if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
        sources.append(prices.oracle_index[encode_a(deriv.underlying_asset)])
    market = prices.market.get(asset_id, None)
    if market != None:
        sources.append(market)
    moving_avg = prices.moving_avg.get(asset_id, None)
    if moving_avg != None:
        sources.append(moving_avg)
    return sum(sources) / len(sources)

####################
# MARGIN COMPUTATION
####################


def get_vault_margin(vault: Vault, prices: Prices, conf: Dict[Asset, AssetConfig], margin: MarginType) -> float:
    """
    A vault's margin is simply the minimum of the values returned by
    - simple margin
    - portfolio margin
    """
    return min(
        get_vault_simple_margin(vault, prices, conf, margin),
        get_vault_portfolio_margin(vault, prices, conf, margin)
    )

# Simple Margin


def get_vault_simple_margin(vault: Vault, prices: Prices, conf: Dict[Asset, AssetConfig], margin: MarginType) -> float:
    """
    simple margin simply sums the margin requirements of each position

    Perpetuals and Futures follow the below formula
      Deriv Notional           = Size * Deriv Mark Price
      Initial Margin Ratio     = 2% + Deriv Notional / $50000
      Maintenance Margin Ratio = 1% + Deriv Notional / $50000
      Margin                   = Deriv Notional * Margin Ratio

    Rationale being that larger positions are more risky, and more difficult to liquidate.
    Hence, larger positions require higher margin ratios, and offer lower leverage.

    Options follow the below formula
      Spot Notional            = Size * Spot Mark Price
      Initial Margin Ratio     = 10%
      Maintenance Margin Ratio =  5%
      Margin                   = Spot Notional * Margin Ratio + Deriv Notional

    Rationale being that there are many options with different strike prices and expiry in the market.
    Hence, a fixed margin ratio is applied per option. 

    The ratios mentioned here can be modified using configs.
    """
    total_margin = 0.0
    for position in vault.positions:
        asset_id = position.asset_id
        # if derivative is already settled, skip to next position. It does not require margin.
        settled_price = get_deriv_settled_price(asset_id, prices)
        if settled_price != None:
            continue
        size = position.balance
        deriv = decode_d(asset_id)
        underlying = deriv.underlying_asset
        deriv_notional = size * get_deriv_mark_price(asset_id, prices)
        c = conf[underlying]
        if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
            fixed_margin = c.future_maintenance_margin if margin.MAINTENANCE else c.future_initial_margin
            # round down to 0.1% AKA 0.001
            variable_margin = round(
                deriv_notional / c.future_variable_margin, 3)
            margin_ratio = min(1.0, fixed_margin + variable_margin)
            total_margin += abs(deriv_notional * margin_ratio)
        elif (deriv.instrument == Instrument.OPTION_CALL or deriv.instrument == Instrument.OPTION_PUT) and size < 0:
            spot_notional = size * \
                get_spot_mark_price(encode_a(underlying), prices)
            fixed_margin = c.option_maintenance_margin if margin.MAINTENANCE else c.option_initial_margin
            total_margin += abs(spot_notional * fixed_margin + deriv_notional)
    return total_margin

# Portfolio Margin


def get_vault_portfolio_margin(vault: Vault, prices: Prices, conf: Dict[Asset, AssetConfig], margin: MarginType) -> float:
    """
    The portfolio margin algorithm uses a Value-at-Risk (VaR) approach to compute margins.
    It simulates the margin requirements by simulating the max loss that the portfolio will
    suffer during spot/volatility movements.

    This computation is relatively straightforward for perpetuals and futures. It simply
    takes spot movement into account, and calculates the position PnL.

    For options, it gets a little more complex. It computes the current implied volatility 
    using the options mark price. Then, applies the black scholes model on top of the 
    simulated spot/vol moves to calculate PnL.

    This PnL simulation assumes 0 correlation between different asset types, it also takes
    on all the assumptions laid out by the Black Scholes Model.

    Simulations are run on an asset level to achieve linear time complexity. O(num_positions).
    """
    # Split vault by asset type O(num_positions)
    position_by_asset: Dict[Asset, List[Position]] = {}
    for position in vault.positions:
        # if derivative is already settled, skip to next position. It does not require margin.
        settled_price = prices.settled.get(position.asset_id, None)
        if settled_price != None:
            continue
        deriv = decode_d(position.asset_id)
        position_by_asset.setdefault(
            deriv.underlying_asset, []).append(position)

    # Calculate max loss per asset type O(num_positions * 4 simulations)
    max_loss_pnl_by_asset: Dict[Asset, float] = {}
    for underlying, positions in position_by_asset.items():
        c = conf[underlying]
        # cache implied volatility computation since its somewhat intensive
        # and identical across simulations. This prevents it from running 4x
        iv_cache: Dict[str, float] = {}
        spot_mark_price = get_spot_mark_price(encode_a(underlying), prices)
        for spot_move in [c.spot_range_simulation, -c.spot_range_simulation]:
            for vol_move in [c.vol_range_simulation, -c.vol_range_simulation]:
                simulation_pnl = 0.0
                for position in positions:
                    asset_id = position.asset_id
                    deriv = decode_d(asset_id)
                    size = position.balance
                    if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
                        simulated_price = spot_mark_price * (1 + spot_move)
                        unit_pnl = simulated_price - spot_mark_price
                        simulation_pnl += size * unit_pnl
                    elif deriv.instrument == Instrument.OPTION_CALL or deriv.instrument == Instrument.OPTION_PUT:
                        mark_price = prices.market[asset_id]
                        flag = 'c' if deriv.instrument == Instrument.OPTION_CALL else 'p'
                        # 8am UTC on expiration_date - unix time now
                        secs_to_expiry = deriv.expiration_date * 86400 + 28800 - NOW
                        years_to_expiry = secs_to_expiry / 31, 536, 000
                        current_iv: float = iv_cache.get(asset_id, implied_volatility(
                            mark_price,
                            spot_mark_price,
                            deriv.strike_price,
                            years_to_expiry,
                            conf[underlying].risk_free_rate,
                            flag
                        ))
                        iv_cache[asset_id] = current_iv
                        simulated_price: float = black_scholes(
                            mark_price,
                            spot_mark_price * (1 + spot_move),
                            deriv.strike_price,
                            current_iv + vol_move,
                            years_to_expiry,
                            conf[underlying].risk_free_rate
                        )
                        unit_pnl = simulated_price - mark_price
                        if deriv.instrument == Instrument.OPTION_CALL:
                            if size > 0:
                                simulation_pnl += max(0, size * unit_pnl)
                            else:
                                simulation_pnl += min(0, size * unit_pnl)
                        else:
                            if size > 0:
                                simulation_pnl -= max(0, size * unit_pnl)
                            else:
                                simulation_pnl -= min(0, size * unit_pnl)
                max_loss_pnl_by_asset[underlying] = min(
                    max_loss_pnl_by_asset[underlying], simulation_pnl)

    # Sum asset max losses
    max_loss_pnl = 0.0
    for asset_pnl in max_loss_pnl_by_asset.values():
        max_loss_pnl += asset_pnl

    # Account for maintenance/initial margin
    if margin == MarginType.INITIAL:
        return PORTFOLIO_MARGIN_INITIAL_MULTIPLIER * abs(max_loss_pnl)
    return abs(max_loss_pnl)

###########################
# VAULT BALANCE COMPUTATION
###########################


def get_vault_balance(vault: Vault, prices: Prices) -> float:
    """
    Sums up collateral balance with current value of all derivative positions 
    """
    balance = vault.collateral_balance
    for position in vault.positions:
        asset_id = position.asset_id
        size = position.balance
        balance += size * get_deriv_mark_price(asset_id, prices)
    return balance


def get_vault_free_collateral(vault: Vault, prices: Prices, conf: Dict[Asset, AssetConfig]) -> float:
    return get_vault_balance(vault, prices) - get_vault_margin(vault, prices, conf, MarginType.MAINTENANCE)


def get_vault_status(vault: Vault, prices: Prices, conf: Dict[Asset, AssetConfig]) -> bool:
    """
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/position/status.cairo#L79
    """
    if get_vault_balance(vault, prices) >= get_vault_margin(vault, prices, conf, MarginType.MAINTENANCE):
        return True  # PerpetualErrorCode.SUCCESS
    return False  # PerpetualErrorCode.OUT_OF_RANGE_TOTAL_RISK

#######################
# DERIVATIVE SETTLEMENT
#######################


def vault_apply_funding(vault: Vault) -> Vault:
    """
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/position/funding.cairo#L73

    Funding is the way in which perpetuals settlement occurs. No changes neccessary.
    """
    return vault


def vault_apply_settlement(vault: Vault, prices: Prices) -> Vault:
    """
    This applies both funding, and options/futures settlement.

    All callsites calling funding now, should call this instead.
    """
    vault = vault_apply_funding(vault)
    new_balance = vault.collateral_balance
    new_positions = []
    for position in vault.positions:
        # if derivative is ready to be settled, settle it
        settled_price = get_deriv_settled_price(position.asset_id, prices)
        if settled_price != None:
            new_balance += position.balance * settled_price
            continue
        new_positions.append(position)
    return Vault(vault.public_key, vault.collateral, new_balance, new_positions)

####################
# TRADE INTERACTIONS
####################


"""
Almost identical to current design, except that it applies two pre-transaction hooks:
1. Perpetual Funding
2. Option & Future Settlement

For all transactions
1. Buyer pays the full value of the deriv trade price to seller in collateral. Buyer receives long position.
2. Seller receives the full value of the deriv trade price from buyer in collateral. Seller receives short position.

This is identical to before

SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/position/update_position.cairo#L58
"""
