from enum import IntEnum
from itertools import product
from typing import Dict, List

from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.implied_volatility import implied_volatility


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


# Generate SAMPLE DATA
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

# Global State (Available in blockchain)
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

# StarkEx Configs


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

# Margin computation


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
        asset_conf = conf[underlying]
        if position.instrument == Instrument.PERPETUAL or position.instrument == Instrument.FUTURE:
            fixed_margin = asset_conf.future_maintenance_margin if margin.MAINTENANCE else asset_conf.future_initial_margin
            variable_margin = round(
                position_usd / asset_conf.future_variable_margin, 3)  # round down to 0.1% AKA 0.001
            margin_ratio = min(1.0, fixed_margin + variable_margin)
            total_margin += position_usd * margin_ratio
        elif (position.instrument == Instrument.OPTION_CALL or position.instrument == Instrument.OPTION_PUT) and size < 0:
            fixed_margin = asset_conf.option_maintenance_margin if margin.MAINTENANCE else asset_conf.option_initial_margin
            total_margin += position_usd * fixed_margin + \
                option_mark_prices[raw_position]
    return total_margin

# Portfolio Margin


def get_spot_move_simulations(conf: Dict[Asset, AssetConfig]) -> List[Dict[Asset, float]]:
    """
    Generates the list of spot move simulations we need to run
    eg. [{BTC: 0.8, ETH: 0.8}, {BTC: 0.8, ETH: 1.2}, {BTC: 1.2, ETH: 0.8}, {BTC: 1.2, ETH: 1.2}]
    """
    # Generates binary cartesian products 00 01 10 11
    simulations = product(range(2), repeat=len(conf))
    output = []
    for trial in simulations:
        i = 0
        trial_output = {}
        for asset, asset_conf in conf.items():
            sim = asset_conf.spot_range_simulation
            trial_output[asset] = 1.0 + sim if trial[i] == 1 else 1.0 - sim
            i += 1
        output.append(trial_output)
    return output


def get_vol_move_simulations(conf: Dict[Asset, AssetConfig]) -> List[Dict[Asset, float]]:
    """
    Generates the list of volatility move simulations we need to run
    eg. [{BTC: -0.45, ETH: -0.45}, {BTC: -0.45, ETH: 0.45}, {BTC: 0.45, ETH: -0.45}, {BTC: 0.45, ETH: 0.45}]
    """
    # Generates binary cartesian products 00 01 10 11
    simulations = product(range(2), repeat=len(conf))
    output = []
    for trial in simulations:
        i = 0
        trial_output = {}
        for asset, asset_conf in conf.items():
            sim = asset_conf.vol_range_simulation
            trial_output[asset] = sim if trial[i] == 1 else -sim
            i += 1
        output.append(trial_output)
    return output


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
    """
    max_loss_pnl = 0.0
    # Spot Range Max Loss
    for spot_move_trial in get_spot_move_simulations(conf):
        # Implied Volatility Max Loss
        for vol_move_trial in get_vol_move_simulations(conf):
            trial_pnl = 0.0
            for raw_position, size in vault.derivative_positions.items():
                position = decode_d(raw_position)
                underlying = position.underlying_asset
                underlying_price = asset_prices[underlying]
                spot_move = spot_move_trial[underlying]
                if position.instrument == Instrument.PERPETUAL or position.instrument == Instrument.FUTURE:
                    simulated_price = underlying_price * spot_move
                    unit_pnl = simulated_price - underlying_price
                    trial_pnl += size * unit_pnl
                elif position.instrument == Instrument.OPTION_CALL or position.instrument == Instrument.OPTION_PUT:
                    vol_move = vol_move_trial[underlying]
                    mark_price = option_mark_prices[raw_position]
                    flag = 'c' if position.instrument == Instrument.OPTION_CALL else 'p'
                    expiration = 0.3  # TODO: remove time to expiration hardcode
                    current_iv: float = implied_volatility(
                        mark_price,
                        underlying_price,
                        position.strike_price,
                        expiration,
                        conf[underlying].risk_free_rate,
                        flag
                    )
                    simulated_price: float = black_scholes(
                        mark_price,
                        underlying_price * spot_move,
                        position.strike_price,
                        current_iv + vol_move,
                        expiration,
                        conf[underlying].risk_free_rate
                    )
                    unit_pnl = simulated_price - mark_price
                    if position.instrument == Instrument.OPTION_CALL:
                        if size > 0:
                            trial_pnl += max(0, size * unit_pnl)
                        else:
                            trial_pnl += min(0, size * unit_pnl)
                    else:
                        if size > 0:
                            trial_pnl -= max(0, size * unit_pnl)
                        else:
                            trial_pnl -= min(0, size * unit_pnl)
            max_loss_pnl = min(max_loss_pnl, trial_pnl)
    return abs(max_loss_pnl)
