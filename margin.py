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


class Derivative:
    def __init__(self, instrument: Instrument, underlying_asset: Asset, resolution: int, expiration_date: int, strike_price: int):
        self.instrument = instrument
        self.underlying_asset = underlying_asset
        self.resolution = resolution  # int4
        # Only for options, and futures
        # unix days (always expires at 8am UTC)
        self.expiration_date = expiration_date
        # Only for options
        self.strike_price = strike_price  # int32


def encode_d(d: Derivative) -> str:
    """Demo Only: This will be replaced to a more efficient encoding in prod"""
    parts = [
        d.instrument.__str__(),
        d.underlying_asset.__str__(),
        d.resolution.__str__(),
        d.expiration_date.__str__(),
        d.strike_price.__str__(),
    ]
    return ":".join(parts)


def decode_d(encoding: str) -> Derivative:
    """Demo Only: This will be replaced to a more efficient encoding in prod"""
    parts = encoding.split(":")
    return Derivative(
        Instrument(parts[0]),
        Asset(parts[1]),
        int(parts[2]),
        int(parts[3]),
        int(parts[4]),
    )


class Position:
    """These float values are resolutionised as ints in StarkEx"""

    def __init__(self, asset_id: str, amount: float, realized_price_index: float):
        # the string encoded form of a derivative
        self.asset_id = asset_id
        # Amount of position held
        self.amount = amount
        # Realized price of the future/perpetual
        # This is the cached funding index for perpetuals
        # This is the average entry price for futures
        self.realized_price_index = realized_price_index


class Vault:
    """These float values are quantized as ints in StarkEx"""

    def __init__(self, collateral: Asset, collateral_amount: float, positions: List[Position]):
        # the collateral asset
        self.collateral = collateral
        # the amount of collateral in the vault
        self.collateral_amount = collateral_amount
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
SAMPLE_VAULT = Vault(Asset.USDC, 1000000, [
    Position(encode_d(SAMPLE_CALL), 1000, 0),
    Position(encode_d(SAMPLE_PUT), 400, 0),
    Position(encode_d(SAMPLE_FUTURE), -100, 1181.12),
    Position(encode_d(SAMPLE_PERP), -500, 1181.12)
]
)


#######################################
# ORACLE STATE (price feed via oracles)
#######################################
ASSET_PRICES: Dict[Asset, float] = {
    Asset.USDC: 1.000,
    Asset.ETH: 1182.42,
    Asset.BTC: 16739.50
}
"""Assume that all option mark prices are available"""
OPTION_MARK_PRICES: Dict[str, float] = {
    encode_d(SAMPLE_CALL): 848.23,
    encode_d(SAMPLE_PUT): 839.12
}


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


####################
# MARGIN COMPUTATION
####################
def get_vault_margin(vault: Vault, asset_prices: Dict[Asset, float], option_mark_prices: Dict[str, float], conf: Dict[Asset, AssetConfig], margin: MarginType) -> float:
    """
    A vault's margin is simply the minimum of the values returned by
    - simple margin
    - portfolio margin
    """
    return min(
        get_vault_simple_margin(
            vault, asset_prices, option_mark_prices, conf, margin
        ),
        get_vault_portfolio_margin(
            vault, asset_prices, option_mark_prices, conf, margin
        )
    )

# Simple Margin


def get_vault_simple_margin(vault: Vault, asset_prices: Dict[Asset, float], option_mark_prices: Dict[str, float], conf: Dict[Asset, AssetConfig], margin: MarginType) -> float:
    """
    simple margin simply sums the margin requirements of each position

    Perpetuals and Futures follow the below formula
      Initial Margin Ratio     = 2% + Position In USD / $50000
      Maintenance Margin Ratio = 1% + Position In USD / $50000
      Margin                   = Position In USD * Margin Ratio

    Rationale being that larger positions are more risky, and more difficult to liquidate.
    Hence, larger positions require higher margin ratios, and offer lower leverage.

    Options follow the below formula
      Initial Margin Ratio     = 10%
      Maintenance Margin Ratio =  5%
      Margin                   = Position In USD * Margin Ratio + Option Mark Price

    Rationale being that there are many options with different strike prices and expiry in the market.
    Hence, a fixed margin ratio is applied per option. 

    The ratios mentioned here can be modified using configs.
    """
    total_margin = 0.0
    for position in vault.positions:
        asset_id = position.asset_id
        size = position.amount
        deriv = decode_d(asset_id)
        underlying = deriv.underlying_asset
        underlying_price = asset_prices[underlying]
        position_usd = size * underlying_price
        c = conf[underlying]
        if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
            fixed_margin = c.future_maintenance_margin if margin.MAINTENANCE else c.future_initial_margin
            # round down to 0.1% AKA 0.001
            variable_margin = round(position_usd / c.future_variable_margin, 3)
            margin_ratio = min(1.0, fixed_margin + variable_margin)
            total_margin += abs(position_usd * margin_ratio)
        elif (deriv.instrument == Instrument.OPTION_CALL or deriv.instrument == Instrument.OPTION_PUT) and size < 0:
            fixed_margin = c.option_maintenance_margin if margin.MAINTENANCE else c.option_initial_margin
            mark_price = option_mark_prices[asset_id]
            total_margin += abs(position_usd * fixed_margin + mark_price)
    return total_margin

# Portfolio Margin


def get_vault_portfolio_margin(vault: Vault, asset_prices: Dict[Asset, float], option_mark_prices: Dict[str, float], conf: Dict[Asset, AssetConfig], margin: MarginType) -> float:
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
        underlying_price = asset_prices[underlying]
        for spot_move in [c.spot_range_simulation, -c.spot_range_simulation]:
            for vol_move in [c.vol_range_simulation, -c.vol_range_simulation]:
                simulation_pnl = 0.0
                for position in positions:
                    asset_id = position.asset_id
                    deriv = decode_d(asset_id)
                    size = position.amount
                    if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
                        simulated_price = underlying_price * (1 + spot_move)
                        unit_pnl = simulated_price - underlying_price
                        simulation_pnl += size * unit_pnl
                    elif deriv.instrument == Instrument.OPTION_CALL or deriv.instrument == Instrument.OPTION_PUT:
                        mark_price = option_mark_prices[asset_id]
                        flag = 'c' if deriv.instrument == Instrument.OPTION_CALL else 'p'
                        # 8am UTC on expiration_date - unix time now
                        secs_to_expiry = deriv.expiration_date * 86400 + 28800 - NOW
                        years_to_expiry = secs_to_expiry / 31, 536, 000
                        current_iv: float = iv_cache.get(asset_id, implied_volatility(
                            mark_price,
                            underlying_price,
                            deriv.strike_price,
                            years_to_expiry,
                            conf[underlying].risk_free_rate,
                            flag
                        ))
                        iv_cache[asset_id] = current_iv
                        simulated_price: float = black_scholes(
                            mark_price,
                            underlying_price * (1 + spot_move),
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


def get_vault_balance(vault: Vault, asset_prices: Dict[Asset, float], option_mark_prices: Dict[str, float]) -> float:
    """
    Applies unrealized PnL on top of vault collateral to get vault balance 
    """
    balance = vault.collateral_amount
    for position in vault.positions:
        asset_id = position.asset_id
        size = position.amount
        realized_price_index = position.realized_price_index
        deriv = decode_d(asset_id)
        underlying = deriv.underlying_asset
        underlying_price = asset_prices[underlying]

        if deriv.instrument == Instrument.PERPETUAL or deriv.instrument == Instrument.FUTURE:
            unit_pnl = realized_price_index - underlying_price
            balance += size * unit_pnl
        elif deriv.instrument == Instrument.OPTION_CALL:
            unit_pnl = option_mark_prices[asset_id]
            if size > 0:
                balance += max(0, size * unit_pnl)
            else:
                balance += min(0, size * unit_pnl)
        elif deriv.instrument == Instrument.OPTION_PUT:
            unit_pnl = option_mark_prices[asset_id]
            if size > 0:
                balance -= max(0, size * unit_pnl)
            else:
                balance -= min(0, size * unit_pnl)

    return balance

####################
# TRADE INTERACTIONS
####################


# def trade(vault: Vault, asset_id: str, collateral_size: float, synthetic_size: float) -> Tuple[Vault, bool]:
#     # if not in vault, simply apply changes

#     # if in vault, options apply changes, perps apply changes,
#     return (vault, True)
