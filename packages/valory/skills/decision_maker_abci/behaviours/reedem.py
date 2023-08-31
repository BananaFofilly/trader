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

"""This module contains the redeeming state of the decision-making abci app."""

from typing import Any, Dict, Generator, List, Optional, Union

from hexbytes import HexBytes
from web3.constants import HASH_ZERO

from packages.valory.contracts.conditional_tokens.contract import (
    ConditionalTokensContract,
)
from packages.valory.contracts.realitio.contract import RealitioContract
from packages.valory.contracts.realitio_proxy.contract import RealitioProxyContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.abstract_round_abci.base import get_name
from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
    WaitableConditionType,
)
from packages.valory.skills.decision_maker_abci.models import MultisendBatch
from packages.valory.skills.decision_maker_abci.payloads import MultisigTxPayload
from packages.valory.skills.decision_maker_abci.redeem_info import (
    Condition,
    FPMM,
    RedeemInfo,
)
from packages.valory.skills.decision_maker_abci.states.redeem import RedeemRound
from packages.valory.skills.market_manager_abci.graph_tooling.requests import (
    FetchStatus,
    QueryingBehaviour,
)


ZERO_HEX = HASH_ZERO[2:]
ZERO_BYTES = bytes.fromhex(ZERO_HEX)
DEFAULT_FROM_BLOCK = "earliest"


class RedeemBehaviour(DecisionMakerBaseBehaviour, QueryingBehaviour):
    """Redeem the winnings."""

    matching_round = RedeemRound

    def __init__(self, **kwargs: Any) -> None:
        """Initialize `RedeemBehaviour`."""
        super().__init__(**kwargs)
        self._already_resolved: bool = False
        self._payouts: Dict[str, int] = {}
        self._from_block: Union[int, str] = DEFAULT_FROM_BLOCK
        self._built_data: Optional[HexBytes] = None
        self._redeem_info: List[RedeemInfo] = []
        self._current_redeem_info: Optional[RedeemInfo] = None
        self._expected_winnings: int = 0

    @property
    def current_redeem_info(self) -> RedeemInfo:
        """Get the current redeem info."""
        if self._current_redeem_info is None:
            raise ValueError("Current redeem information have not been set.")
        return self._current_redeem_info

    @property
    def current_fpmm(self) -> FPMM:
        """Get the current FPMM."""
        return self.current_redeem_info.fpmm

    @property
    def current_condition(self) -> Condition:
        """Get the current condition."""
        return self.current_fpmm.condition

    @property
    def current_question_id(self) -> bytes:
        """Get the current question's id."""
        return self.current_fpmm.question.id

    @property
    def current_collateral_token(self) -> str:
        """Get the current collateral token."""
        return self.current_fpmm.collateralToken

    @property
    def current_condition_id(self) -> HexBytes:
        """Get the current condition id."""
        return self.current_condition.id

    @property
    def current_index_sets(self) -> List[int]:
        """Get the current index sets."""
        return self.current_condition.index_sets

    @property
    def safe_address_lower(self) -> str:
        """Get the safe's address converted to lower case."""
        return self.synchronized_data.safe_contract_address.lower()

    @property
    def payouts(self) -> Dict[str, int]:
        """Get the trades' transaction hashes mapped to payouts for the current market."""
        return self._payouts

    @payouts.setter
    def payouts(self, payouts: Dict[str, int]) -> None:
        """Set the trades' transaction hashes mapped to payouts for the current market."""
        self._payouts = payouts

    @property
    def from_block(self) -> Union[int, str]:
        """Get the fromBlock."""
        return self._from_block

    @from_block.setter
    def from_block(self, from_block: str) -> None:
        """Set the fromBlock."""
        try:
            self._from_block = int(from_block)
        except ValueError:
            self._from_block = DEFAULT_FROM_BLOCK

    @property
    def already_resolved(self) -> bool:
        """Get whether the current market has already been resolved."""
        return self._already_resolved

    @already_resolved.setter
    def already_resolved(self, flag: bool) -> None:
        """Set whether the current market has already been resolved."""
        self._already_resolved = flag

    @property
    def built_data(self) -> HexBytes:
        """Get the built transaction's data."""
        return self._built_data

    @built_data.setter
    def built_data(self, built_data: Union[str, bytes]) -> None:
        """Set the built transaction's data."""
        self._built_data = HexBytes(built_data)

    def _get_redeem_info(
        self,
    ) -> Generator:
        """Fetch the trades from all the prediction markets and store them as redeeming information."""
        while True:
            can_proceed = self._prepare_fetching()
            if not can_proceed:
                break

            trades_market_chunk = yield from self._fetch_redeem_info()
            if trades_market_chunk is not None:
                # here an important assumption is made.
                # we assume that the trade information does not conflict with each other,
                # in the sense that one trade sample can only conclude one redeeming action for one pool.
                # this is correct for the current implementation of the service,
                # because no more than one answer is given to each question.
                # if this were to change, then the multisend transaction prepared below could be incorrect
                # because it would have conflicting calls.
                redeem_updates = [RedeemInfo(**trade) for trade in trades_market_chunk]
                self._redeem_info.extend(redeem_updates)

        if self._fetch_status != FetchStatus.SUCCESS:
            self._redeem_info = []

        self.context.logger.info(f"Fetched redeeming information: {self._redeem_info}")

    def _conditional_tokens_interact(
        self, contract_callable: str, data_key: str, placeholder: str, **kwargs: Any
    ) -> WaitableConditionType:
        """Interact with the conditional tokens contract."""
        status = yield from self.contract_interact(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.conditional_tokens_address,
            contract_public_id=ConditionalTokensContract.contract_id,
            contract_callable=contract_callable,
            data_key=data_key,
            placeholder=placeholder,
            **kwargs,
        )
        return status

    def _check_already_redeemed(self) -> WaitableConditionType:
        """Check whether we have already redeemed for this bet."""
        kwargs: Dict[str, list] = {
            key: []
            for key in (
                "collateral_tokens",
                "parent_collection_ids",
                "condition_ids",
                "index_sets",
                "trade_tx_hashes",
            )
        }
        for redeem_candidate in self._redeem_info:
            kwargs["collateral_tokens"].append(redeem_candidate.fpmm.collateralToken)
            kwargs["parent_collection_ids"].append(ZERO_BYTES)
            kwargs["condition_ids"].append(redeem_candidate.fpmm.condition.id)
            kwargs["index_sets"].append(redeem_candidate.fpmm.condition.index_sets)
            kwargs["trade_tx_hashes"].append(redeem_candidate.transactionHash)

        result = yield from self._conditional_tokens_interact(
            contract_callable="check_redeemed",
            data_key="payouts",
            placeholder=get_name(RedeemBehaviour.payouts),
            redeemer=self.safe_address_lower,
            **kwargs,
        )
        return result

    def _clean_redeem_info(self) -> Generator:
        """Clean the redeeming information based on whether any positions have already been redeemed."""
        yield from self.wait_for_condition_with_sleep(self._check_already_redeemed)
        payout_so_far = sum(self.payouts.values())
        if payout_so_far > 0:
            self._redeem_info = [
                info
                for info in self._redeem_info
                if info.transactionHash not in self.payouts.keys()
            ]
            msg = f"The total payout so far has been {self.wei_to_native(payout_so_far)} wxDAI."
            self.context.logger.info(msg)

    def _is_winning_position(self) -> bool:
        """Return whether the current position is winning."""
        our_answer = self.current_redeem_info.outcomeIndex
        correct_answer = self.current_redeem_info.fpmm.current_answer_index
        return our_answer == correct_answer

    def _is_dust(self) -> bool:
        """Return whether the current claimable amount is dust or not."""
        return self.current_redeem_info.claimable_amount < self.params.dust_threshold

    def _check_already_resolved(self) -> WaitableConditionType:
        """Check whether someone has already resolved for this market."""
        result = yield from self._conditional_tokens_interact(
            contract_callable="check_resolved",
            data_key="resolved",
            placeholder=get_name(RedeemBehaviour.already_resolved),
            condition_id=self.current_condition_id,
        )
        return result

    def _build_resolve_data(self) -> WaitableConditionType:
        """Prepare the safe tx to resolve the condition."""
        result = yield from self.contract_interact(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.realitio_proxy_address,
            contract_public_id=RealitioProxyContract.contract_id,
            contract_callable="build_resolve_tx",
            data_key="data",
            placeholder=get_name(RedeemBehaviour.built_data),
            question_id=self.current_question_id,
            template_id=self.current_fpmm.templateId,
            question=self.current_fpmm.question.data,
            num_outcomes=self.current_condition.outcomeSlotCount,
        )

        if not result:
            return False

        batch = MultisendBatch(
            to=self.params.realitio_proxy_address,
            data=HexBytes(self.built_data),
        )
        self.multisend_batches.append(batch)
        return True

    def _get_block_number(self) -> WaitableConditionType:
        """Get the block number of the current position."""
        market_timestamp = self.current_redeem_info.fpmm.creationTimestamp

        while True:
            block = yield from self._fetch_block_number(market_timestamp)
            if self._fetch_status != FetchStatus.IN_PROGRESS:
                break

        if self._fetch_status == FetchStatus.SUCCESS:
            self.from_block = block.get("id", DEFAULT_FROM_BLOCK)
            self.context.logger.info(
                f"Fetched block number {self.from_block!r} as closest to timestamp {market_timestamp!r}"
            )

        return True

    def _build_claim_data(self) -> WaitableConditionType:
        """Prepare the safe tx to claim the winnings."""
        result = yield from self.contract_interact(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.realitio_address,
            contract_public_id=RealitioContract.contract_id,
            contract_callable="build_claim_winnings",
            data_key="data",
            placeholder=get_name(RedeemBehaviour.built_data),
            from_block=self.from_block,
            question_id=self.current_question_id,
        )

        if not result:
            return False

        batch = MultisendBatch(
            to=self.params.realitio_address,
            data=HexBytes(self.built_data),
        )
        self.multisend_batches.append(batch)
        return True

    def _build_redeem_data(self) -> WaitableConditionType:
        """Prepare the safe tx to redeem the position."""
        result = yield from self._conditional_tokens_interact(
            contract_callable="build_redeem_positions_tx",
            data_key="data",
            placeholder=get_name(RedeemBehaviour.built_data),
            collateral_token=self.current_collateral_token,
            parent_collection_id=ZERO_BYTES,
            condition_id=self.current_condition_id,
            index_sets=self.current_index_sets,
        )

        if not result:
            return False

        batch = MultisendBatch(
            to=self.params.conditional_tokens_address,
            data=HexBytes(self.built_data),
        )
        self.multisend_batches.append(batch)
        return True

    def _prepare_single_redeem(self) -> Generator:
        """Prepare a multisend transaction for a single redeeming action."""
        yield from self.wait_for_condition_with_sleep(self._check_already_resolved)
        steps = [] if self.already_resolved else [self._build_resolve_data]
        steps.extend(
            [
                self._build_claim_data,
                self._build_redeem_data,
            ]
        )

        for build_step in steps:
            yield from self.wait_for_condition_with_sleep(build_step)

    def _process_candidate(
        self, redeem_candidate: RedeemInfo
    ) -> Generator[None, None, bool]:
        """Process a redeeming candidate and return whether winnings were found."""
        self._current_redeem_info = redeem_candidate
        # in case of a non-winning position or the claimable amount is dust
        if not self._is_winning_position() or self._is_dust():
            return False

        yield from self._prepare_single_redeem()
        self._expected_winnings += self.current_redeem_info.claimable_amount
        return True

    def _prepare_safe_tx(self) -> Generator[None, None, Optional[str]]:
        """
        Prepare the safe tx to redeem the positions of the trader.

        Steps:
            1. Get all the trades of the trader.
            2. For each trade, check if the trader has not already redeemed a non-dust winning position.
            3. If so, prepare a multisend transaction like this:
            TXS:
                1. resolve (optional)
                Check if the condition needs to be resolved. If so, add the tx to the multisend.

                2. claimWinnings
                Prepare a claim winnings tx for each winning position. Add it to the multisend.

                3. redeemPositions
                Prepare a redeem positions tx for each winning position. Add it to the multisend.

        We do not convert claimed wxDAI to xDAI, because this is the currency that the service is using to place bets.

        :yields: None
        :returns: the safe's transaction hash for the redeeming operation.
        """
        if len(self._redeem_info) > 0:
            self.context.logger.info("Preparing a multisend tx to redeem payout...")

        winnings_found = False

        for redeem_candidate in self._redeem_info:
            is_non_dust_winning = yield from self._process_candidate(redeem_candidate)
            if not is_non_dust_winning:
                continue

            winnings_found = True

            if len(self.multisend_batches) == self.params.redeeming_batch_size:
                break

        if not winnings_found:
            self.context.logger.info("No winnings to redeem.")
            return None

        for build_step in (
            self._build_multisend_data,
            self._build_multisend_safe_tx_hash,
        ):
            yield from self.wait_for_condition_with_sleep(build_step)

        winnings = self.wei_to_native(self._expected_winnings)
        self.context.logger.info(
            f"Prepared a multisend transaction to redeem winnings of {winnings} wxDAI."
        )
        return self.tx_hex

    def async_act(self) -> Generator:
        """Do the action."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            yield from self._get_redeem_info()
            yield from self._clean_redeem_info()
            agent = self.context.agent_address
            redeem_tx_hex = yield from self._prepare_safe_tx()
            tx_submitter = (
                self.matching_round.auto_round_id()
                if redeem_tx_hex is not None
                else None
            )
            payload = MultisigTxPayload(agent, tx_submitter, redeem_tx_hex)

        yield from self.finish_behaviour(payload)
