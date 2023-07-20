# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2023 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module contains the behaviour for sampling a bet."""

import dataclasses
from typing import Any, Generator, List, cast

from hexbytes import HexBytes

from packages.valory.contracts.erc20.contract import ERC20
from packages.valory.contracts.gnosis_safe.contract import (
    GnosisSafeContract,
    SafeOperation,
)
from packages.valory.contracts.market_maker.contract import (
    FixedProductMarketMakerContract,
)
from packages.valory.contracts.multisend.contract import MultiSendContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
    SAFE_GAS,
    WaitableConditionType,
)
from packages.valory.skills.decision_maker_abci.models import MultisendBatch
from packages.valory.skills.decision_maker_abci.payloads import MultisigTxPayload
from packages.valory.skills.decision_maker_abci.states.bet_placement import (
    BetPlacementRound,
)
from packages.valory.skills.transaction_settlement_abci.payload_tools import (
    hash_payload_to_hex,
)
from packages.valory.skills.transaction_settlement_abci.rounds import TX_HASH_LENGTH


# hardcoded to 0 because we don't need to send any ETH when betting
_ETHER_VALUE = 0


class BetPlacementBehaviour(DecisionMakerBaseBehaviour):
    """A behaviour in which the agents blacklist the sampled bet."""

    matching_round = BetPlacementRound

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the bet placement behaviour."""
        super().__init__(**kwargs)
        self.balance = 0
        self.buy_amount = 0
        self.multisend_batches: List[MultisendBatch] = []
        self.multisend_data = b""
        self.safe_tx_hash = ""

    @property
    def collateral_token(self) -> str:
        """Get the contract address of the token that the market maker supports."""
        return self.synchronized_data.sampled_bet.collateralToken

    @property
    def market_maker_contract_address(self) -> str:
        """Get the contract address of the market maker on which the service is going to place the bet."""
        return self.synchronized_data.sampled_bet.id

    @property
    def investment_amount(self) -> int:
        """Get the investment amount of the bet."""
        return self.params.get_bet_amount(self.synchronized_data.confidence)

    @property
    def sufficient_balance(self) -> int:
        """Get whether the balance is sufficient for the investment amount of the bet."""
        return self.balance >= self.investment_amount

    @property
    def outcome_index(self) -> int:
        """Get the index of the outcome that the service is going to place a bet on."""
        return cast(int, self.synchronized_data.vote)

    @property
    def multi_send_txs(self) -> List[dict]:
        """Get the multisend transactions as a list of dictionaries."""
        return [dataclasses.asdict(batch) for batch in self.multisend_batches]

    def _check_balance(self) -> WaitableConditionType:
        """Check the safe's balance."""
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.collateral_token,
            contract_id=str(ERC20.contract_id),
            contract_callable="check_balance",
            account=self.synchronized_data.safe_contract_address,
        )
        if response_msg.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Could not calculate the balance of the safe: {response_msg}"
            )
            return False

        balance = response_msg.raw_transaction.body.get("balance", None)
        if balance is None:
            self.context.logger.error(
                f"Something went wrong while trying to get the balance of the safe: {response_msg}"
            )
            return False

        self.balance = int(balance)
        return True

    def _build_approval_tx(self) -> WaitableConditionType:
        """Build an ERC20 approve transaction."""
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.collateral_token,
            contract_id=str(ERC20.contract_id),
            contract_callable="build_approval_tx",
            spender=self.market_maker_contract_address,
            amount=self.investment_amount,
        )

        if response_msg.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.info(f"Could not build approval tx: {response_msg}")
            return False

        approval_data = response_msg.state.body.get("data")
        if approval_data is None:
            self.context.logger.info(f"Could not build approval tx: {response_msg}")
            return False

        batch = MultisendBatch(
            to=self.collateral_token,
            data=HexBytes(approval_data),
        )
        self.multisend_batches.append(batch)
        return True

    def _calc_buy_amount(self) -> WaitableConditionType:
        """Calculate the buy amount of the conditional token."""
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.market_maker_contract_address,
            contract_id=str(FixedProductMarketMakerContract.contract_id),
            contract_callable="calc_buy_amount",
            investment_amount=self.investment_amount,
            outcome_index=self.outcome_index,
        )
        if response_msg.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Could not calculate the buy amount: {response_msg}"
            )
            return False

        buy_amount = response_msg.raw_transaction.body.get("amount", None)
        if buy_amount is None:
            self.context.logger.error(
                f"Something went wrong while trying to get the buy amount for the conditional token: {response_msg}"
            )
            return False

        self.buy_amount = int(buy_amount)
        return True

    def _build_buy_tx(self) -> WaitableConditionType:
        """Get the buy tx data encoded."""
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.market_maker_contract_address,
            contract_id=str(FixedProductMarketMakerContract.contract_id),
            contract_callable="get_buy_data",
            investment_amount=self.investment_amount,
            outcome_index=self.outcome_index,
            min_outcome_tokens_to_buy=self.buy_amount,
        )
        if response_msg.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.error(
                f"Could not get the data for the buy transaction: {response_msg}"
            )
            return False

        buy_data = response_msg.state.body.get("data", None)
        if buy_data is None:
            self.context.logger.error(
                f"Something went wrong while trying to encode the buy data: {response_msg}"
            )
            return False

        batch = MultisendBatch(
            to=self.market_maker_contract_address,
            data=HexBytes(buy_data),
        )
        self.multisend_batches.append(batch)
        return True

    def _build_multisend_data(
        self,
    ) -> WaitableConditionType:
        """Get the multisend tx."""
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.multisend_address,
            contract_id=str(MultiSendContract.contract_id),
            contract_callable="get_tx_data",
            multi_send_txs=self.multi_send_txs,
        )
        expected_performative = ContractApiMessage.Performative.RAW_TRANSACTION
        if response_msg.performative != expected_performative:
            self.context.logger.error(
                f"Couldn't compile the multisend tx. "
                f"Expected response performative {expected_performative.value}, "  # type: ignore
                f"received {response_msg.performative.value}: {response_msg}"
            )
            return False

        multisend_data_str = response_msg.raw_transaction.body.get("data", None)
        if multisend_data_str is None:
            self.context.logger.error(
                f"Something went wrong while trying to prepare the multisend data: {response_msg}"
            )
            return False

        # strip "0x" from the response
        multisend_data_str = str(response_msg.raw_transaction.body["data"])[2:]
        self.multisend_data = bytes.fromhex(multisend_data_str)
        return True

    def _build_safe_tx_hash(self) -> WaitableConditionType:
        """Prepares and returns the safe tx hash."""
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.synchronized_data.safe_contract_address,
            contract_id=str(GnosisSafeContract.contract_id),
            contract_callable="get_raw_safe_transaction_hash",
            to_address=self.params.multisend_address,
            value=_ETHER_VALUE,
            data=self.multisend_data,
            safe_tx_gas=SAFE_GAS,
            operation=SafeOperation.DELEGATE_CALL.value,
        )

        if response_msg.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.error(
                "Couldn't get safe tx hash. Expected response performative "
                f"{ContractApiMessage.Performative.STATE.value}, "  # type: ignore
                f"received {response_msg.performative.value}: {response_msg}."
            )
            return False

        tx_hash = response_msg.state.body.get("tx_hash", None)
        if tx_hash is None or len(tx_hash) != TX_HASH_LENGTH:
            self.context.logger.error(
                "Something went wrong while trying to get the buy transaction's hash. "
                f"Invalid hash {tx_hash!r} was returned."
            )
            return False

        # strip "0x" from the response hash
        self.safe_tx_hash = tx_hash[2:]
        return True

    def _prepare_safe_tx(self) -> Generator[None, None, str]:
        """Prepare the safe transaction for placing a bet and return the hex for the tx settlement skill."""
        for step in (
            self._build_approval_tx,
            self._calc_buy_amount,
            self._build_buy_tx,
            self._build_multisend_data,
            self._build_safe_tx_hash,
        ):
            yield from self.wait_for_condition_with_sleep(step)

        return hash_payload_to_hex(
            self.safe_tx_hash,
            _ETHER_VALUE,
            SAFE_GAS,
            self.params.multisend_address,
            self.multisend_data,
            SafeOperation.DELEGATE_CALL.value,
        )

    def async_act(self) -> Generator:
        """Do the action."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            yield from self.wait_for_condition_with_sleep(self._check_balance)
            betting_tx_hex = None
            if self.sufficient_balance:
                betting_tx_hex = yield from self._prepare_safe_tx()
            payload = MultisigTxPayload(self.context.agent_address, betting_tx_hex)

        yield from self.finish_behaviour(payload)
