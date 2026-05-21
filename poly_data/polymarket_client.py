from dotenv import load_dotenv          # Environment variable management
import os                           # Operating system interface

# Polymarket API client libraries (CLOB V2)
from py_clob_client_v2 import (
    ClobClient,
    OrderArgs,
    OrderType,
    Side,
    SignatureTypeV2,
    BalanceAllowanceParams,
    AssetType,
    PartialCreateOrderOptions,
)
from py_clob_client_v2.constants import POLYGON
from py_clob_client_v2.clob_types import OrderMarketCancelParams

# Web3 libraries for blockchain interaction
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

import requests                     # HTTP requests
import pandas as pd                 # Data analysis
import json                         # JSON processing
import subprocess                   # For calling external processes

from py_clob_client_v2 import OpenOrderParams

# Smart contract ABIs
from poly_data.abis import NegRiskAdapterABI, ConditionalTokenABI, erc20_abi

# Load environment variables
load_dotenv()


class PolymarketClient:
    """
    Client for interacting with Polymarket's API and smart contracts.
    
    This class provides methods for:
    - Creating and managing orders
    - Querying order book data
    - Checking balances and positions
    - Merging positions
    
    The client connects to both the Polymarket API and the Polygon blockchain.
    """
    
    def __init__(self, pk='default') -> None:
        """
        Initialize the Polymarket client with API and blockchain connections.
        
        Args:
            pk (str, optional): Private key identifier, defaults to 'default'
        """
        host="https://clob.polymarket.com"

        # Get credentials from environment variables
        key=os.getenv("PK")
        browser_address = os.getenv("BROWSER_ADDRESS")
        self.dry_run = str(os.getenv("DRY_RUN", "false")).lower() in ("1", "true", "yes", "on")

        # Don't print sensitive wallet information
        print("Initializing Polymarket client...")
        if self.dry_run:
            print("[DRY_RUN] Enabled: orders/cancels/merges will be logged but NOT executed")
        chain_id=POLYGON
        self.browser_wallet=Web3.to_checksum_address(browser_address)

        # Initialize the Polymarket API client (CLOB V2 with EIP-1271 signatures)
        # Two-step: derive API creds via L1 client, then create fully-auth client.
        _bootstrap = ClobClient(
            host=host,
            key=key,
            chain_id=chain_id,
            funder=self.browser_wallet,
            signature_type=SignatureTypeV2.POLY_1271,
        )
        self.creds = _bootstrap.create_or_derive_api_key()
        self.client = ClobClient(
            host=host,
            key=key,
            chain_id=chain_id,
            creds=self.creds,
            funder=self.browser_wallet,
            signature_type=SignatureTypeV2.POLY_1271,
        )
        
        # Initialize Web3 connection to Polygon
        # POLYGON_RPC_URL overrides the public endpoint (which has become 401/disabled)
        rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
        web3 = Web3(Web3.HTTPProvider(rpc_url))
        web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        
        # Set up USDC contract for balance checks
        self.usdc_contract = web3.eth.contract(
            address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 
            abi=erc20_abi
        )

        # Store key contract addresses
        self.addresses = {
            'neg_risk_adapter': '0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296',
            'collateral': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
            'conditional_tokens': '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045'
        }

        # Initialize contract interfaces
        self.neg_risk_adapter = web3.eth.contract(
            address=self.addresses['neg_risk_adapter'], 
            abi=NegRiskAdapterABI
        )

        self.conditional_tokens = web3.eth.contract(
            address=self.addresses['conditional_tokens'], 
            abi=ConditionalTokenABI
        )

        self.web3 = web3

    
    def create_order(self, marketId, action, price, size, neg_risk=False):
        """
        Create and submit a new order to the Polymarket order book.
        
        Args:
            marketId (str): ID of the market token to trade
            action (str): "BUY" or "SELL"
            price (float): Order price (0-1 range for prediction markets)
            size (float): Order size in USDC
            neg_risk (bool, optional): Whether this is a negative risk market. Defaults to False.
            
        Returns:
            dict: Response from the API containing order details, or empty dict on error
        """
        # Create order parameters (V2: side must be Side enum, not raw string)
        side_enum = Side.BUY if str(action).upper() == "BUY" else Side.SELL
        order_args = OrderArgs(
            token_id=str(marketId),
            price=price,
            size=size,
            side=side_enum,
        )

        # Circuit-breaker check: refuse new BUYs once today's loss caps trip.
        # SELLs are exempt because we ALWAYS need to be able to exit risk.
        if side_enum == Side.BUY:
            try:
                import PM_circuit_breakers as _cb
                blocked, reason = _cb.should_block_buy()
                if blocked:
                    print(f"[CIRCUIT] BUY blocked: {reason}")
                    return {"circuit_blocked": True, "reason": reason,
                            "token_id": str(marketId), "side": action,
                            "price": price, "size": size}
            except Exception as _cb_err:
                print(f"[circuit] check failed (continuing): {_cb_err}")

        if self.dry_run:
            print(
                f"[DRY_RUN] Would post order: token={marketId} side={action} "
                f"price={price} size={size} neg_risk={neg_risk}"
            )
            # Paper-fill simulator: record the order so a later check_fills()
            # in trading.py can mark it as filled when the real book crosses.
            try:
                import PM_paper_fills as _pf
                _pf.record_post(marketId, action, price, size)
            except Exception as _pf_err:
                print(f"[paper] record_post failed: {_pf_err}")
            return {
                "dry_run": True,
                "token_id": str(marketId),
                "side": action,
                "price": price,
                "size": size,
                "neg_risk": neg_risk,
            }

        # V2 combines create+post; SDK auto-resolves neg_risk/tick_size from token_id
        try:
            options = PartialCreateOrderOptions(neg_risk=True) if neg_risk else None
            resp = self.client.create_and_post_order(
                order_args=order_args,
                options=options,
                order_type=OrderType.GTC,
            )
            return resp
        except Exception as ex:
            print(ex)
            return {}

    def get_order_book(self, market):
        """
        Get the current order book for a specific market.
        
        Args:
            market (str): Market ID to query
            
        Returns:
            tuple: (bids_df, asks_df) - DataFrames containing bid and ask orders
        """
        orderBook = self.client.get_order_book(market)
        return pd.DataFrame(orderBook.bids).astype(float), pd.DataFrame(orderBook.asks).astype(float)


    def get_usdc_balance(self):
        """
        Get the USDC balance of the connected wallet.
        
        Returns:
            float: USDC balance in decimal format
        """
        return self.usdc_contract.functions.balanceOf(self.browser_wallet).call() / 10**6
     
    def get_pos_balance(self):
        """
        Get the total value of all positions for the connected wallet.
        
        Returns:
            float: Total position value in USDC
        """
        res = requests.get(f'https://data-api.polymarket.com/value?user={self.browser_wallet}')
        return float(res.json()['value'])

    def get_total_balance(self):
        """
        Get the combined value of USDC balance and all positions.
        
        Returns:
            float: Total account value in USDC
        """
        return self.get_usdc_balance() + self.get_pos_balance()

    def get_all_positions(self):
        """
        Get all positions for the connected wallet across all markets.
        
        Returns:
            DataFrame: All positions with details like market, size, avgPrice
        """
        res = requests.get(f'https://data-api.polymarket.com/positions?user={self.browser_wallet}')
        return pd.DataFrame(res.json())
    
    def get_raw_position(self, tokenId):
        """
        Get the raw token balance for a specific market outcome token.
        
        Args:
            tokenId (int): Token ID to query
            
        Returns:
            int: Raw token amount (before decimal conversion)
        """
        return int(self.conditional_tokens.functions.balanceOf(self.browser_wallet, int(tokenId)).call())

    def get_position(self, tokenId):
        """
        Get both raw and formatted position size for a token.
        
        Args:
            tokenId (int): Token ID to query
            
        Returns:
            tuple: (raw_position, shares) - Raw token amount and decimal shares
                   Shares less than 1 are treated as 0 to avoid dust amounts
        """
        raw_position = self.get_raw_position(tokenId)
        shares = float(raw_position / 1e6)

        # Ignore very small positions (dust)
        if shares < 1:
            shares = 0

        return raw_position, shares
    
    def get_all_orders(self):
        """
        Get all open orders for the connected wallet.
        
        Returns:
            DataFrame: All open orders with their details
        """
        orders_df = pd.DataFrame(self.client.get_open_orders())

        # Convert numeric columns to float
        for col in ['original_size', 'size_matched', 'price']:
            if col in orders_df.columns:
                orders_df[col] = orders_df[col].astype(float)

        return orders_df

    def get_market_orders(self, market):
        """
        Get all open orders for a specific market.
        
        Args:
            market (str): Market ID to query
            
        Returns:
            DataFrame: Open orders for the specified market
        """
        orders_df = pd.DataFrame(self.client.get_open_orders(
            OpenOrderParams(market=market)
        ))

        # Convert numeric columns to float
        for col in ['original_size', 'size_matched', 'price']:
            if col in orders_df.columns:
                orders_df[col] = orders_df[col].astype(float)

        return orders_df
    

    def cancel_all_asset(self, asset_id):
        """
        Cancel all orders for a specific asset token.
        
        Args:
            asset_id (str): Asset token ID
        """
        if self.dry_run:
            print(f"[DRY_RUN] Would cancel all orders for asset={asset_id}")
            try:
                import PM_paper_fills as _pf
                _pf.record_cancel(asset_id)
            except Exception as _pf_err:
                print(f"[paper] record_cancel failed: {_pf_err}")
            return
        self.client.cancel_market_orders(
            OrderMarketCancelParams(asset_id=str(asset_id))
        )


    
    def cancel_all_market(self, marketId):
        """
        Cancel all orders in a specific market.
        
        Args:
            marketId (str): Market ID
        """
        if self.dry_run:
            print(f"[DRY_RUN] Would cancel all orders for market={marketId}")
            return
        self.client.cancel_market_orders(
            OrderMarketCancelParams(market=marketId)
        )

    
    def merge_positions(self, amount_to_merge, condition_id, is_neg_risk_market):
        """
        Merge positions in a market to recover collateral.
        
        This function calls the external poly_merger Node.js script to execute
        the merge operation on-chain. When you hold both YES and NO positions
        in the same market, merging them recovers your USDC.
        
        Args:
            amount_to_merge (int): Raw token amount to merge (before decimal conversion)
            condition_id (str): Market condition ID
            is_neg_risk_market (bool): Whether this is a negative risk market
            
        Returns:
            str: Transaction hash or output from the merge script
            
        Raises:
            Exception: If the merge operation fails
        """
        amount_to_merge_str = str(amount_to_merge)
        neg_risk_arg = "true" if is_neg_risk_market else "false"

        if self.dry_run:
            print(
                f"[DRY_RUN] Would merge positions: amount={amount_to_merge_str} "
                f"condition_id={condition_id} neg_risk={neg_risk_arg}"
            )
            return "dry_run_merge_skipped"

        # Prepare command args to avoid shell parsing issues.
        node_command = ["node", "poly_merger/merge.js", amount_to_merge_str, str(condition_id), neg_risk_arg]
        print("Running merge command:", " ".join(node_command))

        # Run the command and capture the output
        result = subprocess.run(node_command, capture_output=True, text=True)
        
        # Check if there was an error
        if result.returncode != 0:
            print("Error:", result.stderr)
            raise Exception(f"Error in merging positions: {result.stderr}")
        
        print("Done merging")

        # Return the transaction hash or output
        return result.stdout