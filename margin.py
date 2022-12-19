from enum import IntEnum
from itertools import product
from typing import Dict, List

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
    def __init__(self, instrument: Instrument, underlying_asset: Asset, resolution: int, cash_asset: Asset, expiration_date: int, strike_price: int):
        self.instrument = instrument
        self.underlying_asset = underlying_asset
        self.resolution = resolution  # int4
        self.cash_asset = cash_asset
        self.expiration_date = expiration_date  # timestamp
        self.strike_price = strike_price  # int32


def encode_d(d: Derivative) -> str:
    """Demo Only: This will be replaced to a more efficient encoding in prod"""
    parts = [
        d.instrument.__str__(),
        d.underlying_asset.__str__(),
        d.resolution.__str__(),
        d.cash_asset.__str__(),
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
        Asset(parts[3]),
        int(parts[4]),
        int(parts[5]),
    )


class Vault:
    """These float values are quantized as ints in StarkEx"""

    def __init__(self, collateral_amount: float, derivative_positions: Dict[str, float]):
        self.collateral_amount = collateral_amount
        self.derivative_positions = derivative_positions


##############
# SAMPLE VAULT
##############
sample_call = Derivative(
    Instrument.OPTION_CALL,
    Asset.ETH, 4, Asset.USDC, 1234, 1200
)
sample_put = Derivative(
    Instrument.OPTION_PUT,
    Asset.ETH, 4, Asset.USDC, 1234, 1100
)
sample_future = Derivative(
    Instrument.FUTURE,
    Asset.ETH, 4, Asset.USDC, 1234, 1200
)
sample_perp = Derivative(
    Instrument.PERPETUAL,
    Asset.ETH, 4, Asset.USDC, 0, 0
)
sample_vault = Vault(1000000, {
    encode_d(sample_call): 1000,
    encode_d(sample_put): 400,
    encode_d(sample_future): -100,
    encode_d(sample_perp): - 500
})


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
    encode_d(sample_call): 848.23,
    encode_d(sample_put): 839.12
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
    for raw_position, size in vault.derivative_positions.items():
        position = decode_d(raw_position)
        underlying = position.underlying_asset
        underlying_price = asset_prices[underlying]
        position_usd = size * underlying_price
        c = conf[underlying]
        if position.instrument == Instrument.PERPETUAL or position.instrument == Instrument.FUTURE:
            fixed_margin = c.future_maintenance_margin if margin.MAINTENANCE else c.future_initial_margin
            # round down to 0.1% AKA 0.001
            variable_margin = round(position_usd / c.future_variable_margin, 3)
            margin_ratio = min(1.0, fixed_margin + variable_margin)
            total_margin += abs(position_usd * margin_ratio)
        elif (position.instrument == Instrument.OPTION_CALL or position.instrument == Instrument.OPTION_PUT) and size < 0:
            fixed_margin = c.option_maintenance_margin if margin.MAINTENANCE else c.option_initial_margin
            mark_price = option_mark_prices[raw_position]
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
    position_by_asset: Dict[Asset, List[str]] = {}
    for raw_position in vault.derivative_positions.keys():
        position = decode_d(raw_position)
        position_by_asset.setdefault(
            position.underlying_asset, []).append(raw_position)

    # Calculate max loss per asset type O(num_positions * 4 simulations)
    max_loss_pnl_by_asset: Dict[Asset, float] = {}
    for underlying, raw_position_list in position_by_asset.items():
        c = conf[underlying]
        # cache implied volatility computation since its somewhat intensive
        # and identical across simulations. This prevents it from running 4x
        iv_cache: Dict[str, float] = {}
        underlying_price = asset_prices[underlying]
        for spot_move in [c.spot_range_simulation, -c.spot_range_simulation]:
            for vol_move in [c.vol_range_simulation, -c.vol_range_simulation]:
                simulation_pnl = 0.0
                for raw_position in raw_position_list:
                    position = decode_d(raw_position)
                    size = vault.derivative_positions[raw_position]
                    if position.instrument == Instrument.PERPETUAL or position.instrument == Instrument.FUTURE:
                        simulated_price = underlying_price * (1 + spot_move)
                        unit_pnl = simulated_price - underlying_price
                        simulation_pnl += size * unit_pnl
                    elif position.instrument == Instrument.OPTION_CALL or position.instrument == Instrument.OPTION_PUT:
                        mark_price = option_mark_prices[raw_position]
                        flag = 'c' if position.instrument == Instrument.OPTION_CALL else 'p'
                        expiration = 0.3  # TODO: remove time to expiration hardcode
                        current_iv: float = iv_cache.get(raw_position, implied_volatility(
                            mark_price,
                            underlying_price,
                            position.strike_price,
                            expiration,
                            conf[underlying].risk_free_rate,
                            flag
                        ))
                        iv_cache[raw_position] = current_iv
                        simulated_price: float = black_scholes(
                            mark_price,
                            underlying_price * (1 + spot_move),
                            position.strike_price,
                            current_iv + vol_move,
                            expiration,
                            conf[underlying].risk_free_rate
                        )
                        unit_pnl = simulated_price - mark_price
                        if position.instrument == Instrument.OPTION_CALL:
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

#
