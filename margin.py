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
        self.instrument = instrument #int2
        self.underlying_asset = underlying_asset # int16
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
    """

    def __init__(self, public_key: str, collateral: Asset, collateral_balance: float, positions: List[Position]):
        self.public_key = public_key
        # the collateral asset
        self.collateral = collateral
        # the balance of collateral in the vault
        self.collateral_balance = collateral_balance
        # list of positions
        self.positions = positions


##############
# SAMPLE VAULT
##############
NOW = round(time())
UNIX_DAYS_NOW = NOW // 86400
UNIX_DAYS_30_DAYS = UNIX_DAYS_NOW + 30

SAMPLE_CALL = Derivative(
    Instrument.OPTION_CALL,
    Asset.ETH, 4, UNIX_DAYS_30_DAYS, 1200
)
SAMPLE_PUT = Derivative(
    Instrument.OPTION_PUT,
    Asset.ETH, 4, UNIX_DAYS_30_DAYS, 1100
)
SAMPLE_FUTURE = Derivative(
    Instrument.FUTURE,
    Asset.ETH, 4, UNIX_DAYS_30_DAYS, 0
)
SAMPLE_PERP = Derivative(
    Instrument.PERPETUAL,
    Asset.ETH, 4,  0, 0
)
SAMPLE_VAULT = Vault("0xdeadbeef", Asset.USDC, 1000000, [
    Position(encode_d(SAMPLE_CALL), 1000, 0),
    Position(encode_d(SAMPLE_PUT), 400, 0),
    Position(encode_d(SAMPLE_FUTURE), -100, 1181.12),
    Position(encode_d(SAMPLE_PERP), -500, 1181.12)
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
This is required for options where index prices are not usable.
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


class Prices:
    def __init__(self, oracle_index: Dict[str, float], market: Dict[str, float], moving_avg: Dict[str, float]):
        self.oracle_index = oracle_index
        self.market = market
        self.moving_avg = moving_avg


PRICES = Prices(ORACLE_INDEX_PRICES, MARKET_PRICES,  MOVING_AVERAGE_PRICES)

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
    sources = [prices.oracle_index[asset_id]]
    market = prices.market.get(asset_id, 0.0)
    if market > 0.0:
        sources.append(market)
    moving_avg = prices.moving_avg.get(asset_id, 0.0)
    if moving_avg > 0.0:
        sources.append(moving_avg)
    return sum(sources) / len(sources)


def get_deriv_mark_price(asset_id: str, prices: Prices) -> float:
    """
    Mark Price is the simple average of all three price sources (whenever available)
    Index Price is only used for futures and perpetuals
    """
    deriv = decode_d(asset_id)
    sources = []
    if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
        sources.append(prices.oracle_index[encode_a(deriv.underlying_asset)])
    market = prices.market.get(asset_id, 0.0)
    if market > 0.0:
        sources.append(market)
    moving_avg = prices.moving_avg.get(asset_id, 0.0)
    if moving_avg > 0.0:
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
      Spot Notional            = Size * Spot Mark Price
      Initial Margin Ratio     = 2% + Spot Notional / $50000
      Maintenance Margin Ratio = 1% + Spot Notional / $50000
      Margin                   = Size * Deriv Mark Price * Margin Ratio

    Rationale being that larger positions are more risky, and more difficult to liquidate.
    Hence, larger positions require higher margin ratios, and offer lower leverage.

    Options follow the below formula
      Initial Margin Ratio     = 10%
      Maintenance Margin Ratio =  5%
      Margin                   = Spot Notional * Margin Ratio + Option Mark Price

    Rationale being that there are many options with different strike prices and expiry in the market.
    Hence, a fixed margin ratio is applied per option. 

    The ratios mentioned here can be modified using configs.
    """
    total_margin = 0.0
    for position in vault.positions:
        asset_id = position.asset_id
        size = position.balance
        deriv = decode_d(asset_id)
        underlying = deriv.underlying_asset
        spot_mark_price = get_spot_mark_price(encode_a(underlying), prices)
        spot_notional = size * spot_mark_price
        c = conf[underlying]
        if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
            fixed_margin = c.future_maintenance_margin if margin.MAINTENANCE else c.future_initial_margin
            # round down to 0.1% AKA 0.001
            variable_margin = round(
                spot_notional / c.future_variable_margin, 3)
            margin_ratio = min(1.0, fixed_margin + variable_margin)
            deriv_mark_price = get_deriv_mark_price(asset_id, prices)
            total_margin += abs(size * deriv_mark_price * margin_ratio)
        elif (deriv.instrument == Instrument.OPTION_CALL or deriv.instrument == Instrument.OPTION_PUT) and size < 0:
            fixed_margin = c.option_maintenance_margin if margin.MAINTENANCE else c.option_initial_margin
            deriv_mark_price = get_deriv_mark_price(asset_id, prices)
            total_margin += abs(spot_notional *
                                fixed_margin + deriv_mark_price)
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
    Applies unrealized PnL on top of vault collateral to get vault balance 
    """
    balance = vault.collateral_balance
    for position in vault.positions:
        asset_id = position.asset_id
        size = position.balance
        deriv = decode_d(asset_id)
        if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
            balance += size * get_deriv_mark_price(asset_id, prices)
        elif deriv.instrument == Instrument.OPTION_CALL:
            value = size * get_deriv_mark_price(asset_id, prices)
            if size > 0:
                balance += max(0, value)
            else:
                balance += min(0, value)
        elif deriv.instrument == Instrument.OPTION_PUT:
            value = size * get_deriv_mark_price(asset_id, prices)
            if size > 0:
                balance -= max(0, value)
            else:
                balance -= min(0, value)
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

####################
# TRADE INTERACTIONS
####################


class OrderBase:
    """
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/exchange/cairo/order.cairo#L2
    """

    def __init__(self, nonce: int, public_key: str, expiration_timestamp: int, signature_r: str, signature_s: str):
        self.nonce = nonce
        self.public_key = public_key
        self.expiration_timestamp = expiration_timestamp
        self.signature_r = signature_r
        self.signature_s = signature_s


class LimitOrder:
    """
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/order/limit_order.cairo#L10
    """

    def __init__(self, base: OrderBase, balance_synthetic: int, balance_collateral: int, balance_fee: int, asset_id_synthetic: int, asset_id_collateral: Asset, position_id: Vault, is_buying_synthetic: bool):
        self.base = base
        self.balance_synthetic = balance_synthetic
        self.balance_collateral = balance_collateral
        self.balance_fee = balance_fee
        self.asset_id_synthetic = asset_id_synthetic
        self.asset_id_collateral = asset_id_collateral
        self.position_id = position_id
        self.is_buying_synthetic = is_buying_synthetic


def execute_limit_order(limit_order: LimitOrder, vault: Vault, actual_collateral: int,  actual_synthetic: int, actual_fee: float) -> Tuple[Vault, bool]:
    """
    SRC: https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/transactions/execute_limit_order.cairo#L30

    Almost identical to current design, except that it applies two pre-transaction hooks:
    1. Perpetual Funding
    2. Option & Future Settlement

    https://github.com/starkware-libs/stark-perpetual/blob/master/src/services/perpetual/cairo/position/update_position.cairo#L58
    """

    # Check that asset_id_collateral is collateral.
    if limit_order.asset_id_collateral != vault.collateral:
        return (vault, False)  # PerpetualErrorCode.INVALID_COLLATERAL_ASSET_ID

    # local collateral_delta
    # local synthetic_delta
    # if limit_order.is_buying_synthetic != 0:
    #     assert collateral_delta = (-actual_collateral) - actual_fee
    #     assert synthetic_delta = actual_synthetic
    # else:
    #     assert collateral_delta = actual_collateral - actual_fee
    #     assert synthetic_delta = -actual_synthetic
    # end

    # if not in vault, simply apply changes

    # if in vault, options apply changes, perps apply changes,
    return (vault, True)
