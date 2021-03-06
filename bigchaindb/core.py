import random
import math
import collections
from time import time

from itertools import compress
from bigchaindb import exceptions as core_exceptions
from bigchaindb.common import crypto, exceptions
from bigchaindb.common.utils import gen_timestamp, serialize
from bigchaindb.common.transaction import TransactionLink

import bigchaindb

from bigchaindb import backend, config_utils, utils
from bigchaindb.consensus import BaseConsensusRules
from bigchaindb.models import Block, Transaction


class Bigchain(object):
    """Bigchain API

    Create, read, sign, write transactions to the database
    """

    # return if a block has been voted invalid
    BLOCK_INVALID = 'invalid'
    # return if a block is valid, or tx is in valid block
    BLOCK_VALID = TX_VALID = 'valid'
    # return if block is undecided, or tx is in undecided block
    BLOCK_UNDECIDED = TX_UNDECIDED = 'undecided'
    # return if transaction is in backlog
    TX_IN_BACKLOG = 'backlog'

    def __init__(self, public_key=None, private_key=None, keyring=[], connection=None, backlog_reassign_delay=None):
        """Initialize the Bigchain instance

        A Bigchain instance has several configuration parameters (e.g. host).
        If a parameter value is passed as an argument to the Bigchain
        __init__ method, then that is the value it will have.
        Otherwise, the parameter value will come from an environment variable.
        If that environment variable isn't set, then the value
        will come from the local configuration file. And if that variable
        isn't in the local configuration file, then the parameter will have
        its default value (defined in bigchaindb.__init__).

        Args:
            public_key (str): the base58 encoded public key for the ED25519 curve.
            private_key (str): the base58 encoded private key for the ED25519 curve.
            keyring (list[str]): list of base58 encoded public keys of the federation nodes.
            connection (:class:`~bigchaindb.backend.connection.Connection`):
                A connection to the database.
        """

        config_utils.autoconfigure()

        self.me = public_key or bigchaindb.config['keypair']['public']
        self.me_private = private_key or bigchaindb.config['keypair']['private']
        self.nodes_except_me = keyring or bigchaindb.config['keyring']

        if backlog_reassign_delay is None:
            backlog_reassign_delay = bigchaindb.config['backlog_reassign_delay']
        self.backlog_reassign_delay = backlog_reassign_delay

        consensusPlugin = bigchaindb.config.get('consensus_plugin')

        if consensusPlugin:
            self.consensus = config_utils.load_consensus_plugin(consensusPlugin)
        else:
            self.consensus = BaseConsensusRules

        self.connection = connection if connection else backend.connect(**bigchaindb.config['database'])
        if not self.me or not self.me_private:
            raise exceptions.KeypairNotFoundException()

    def write_transaction(self, signed_transaction):
        """Write the transaction to bigchain.

        When first writing a transaction to the bigchain the transaction will be kept in a backlog until
        it has been validated by the nodes of the federation.

        Args:
            signed_transaction (Transaction): transaction with the `signature` included.

        Returns:
            dict: database response
        """
        signed_transaction = signed_transaction.to_dict()

        # we will assign this transaction to `one` node. This way we make sure that there are no duplicate
        # transactions on the bigchain
        if self.nodes_except_me:
            assignee = random.choice(self.nodes_except_me)
        else:
            # I am the only node
            assignee = self.me

        signed_transaction.update({'assignee': assignee})
        signed_transaction.update({'assignment_timestamp': time()})

        # write to the backlog
        return backend.query.write_transaction(self.connection, signed_transaction)

    def reassign_transaction(self, transaction):
        """Assign a transaction to a new node

        Args:
            transaction (dict): assigned transaction

        Returns:
            dict: database response or None if no reassignment is possible
        """

        if self.nodes_except_me:
            try:
                federation_nodes = self.nodes_except_me + [self.me]
                index_current_assignee = federation_nodes.index(transaction['assignee'])
                new_assignee = random.choice(federation_nodes[:index_current_assignee] +
                                             federation_nodes[index_current_assignee + 1:])
            except ValueError:
                # current assignee not in federation
                new_assignee = random.choice(self.nodes_except_me)

        else:
            # There is no other node to assign to
            new_assignee = self.me

        return backend.query.update_transaction(
                self.connection, transaction['id'],
                {'assignee': new_assignee, 'assignment_timestamp': time()})

    def delete_transaction(self, *transaction_id):
        """Delete a transaction from the backlog.

        Args:
            *transaction_id (str): the transaction(s) to delete

        Returns:
            The database response.
        """

        return backend.query.delete_transaction(self.connection, *transaction_id)

    def get_stale_transactions(self):
        """Get a cursor of stale transactions.

        Transactions are considered stale if they have been assigned a node, but are still in the
        backlog after some amount of time specified in the configuration
        """

        return backend.query.get_stale_transactions(self.connection, self.backlog_reassign_delay)

    def validate_transaction(self, transaction):
        """Validate a transaction.

        Args:
            transaction (Transaction): transaction to validate.

        Returns:
            The transaction if the transaction is valid else it raises an
            exception describing the reason why the transaction is invalid.
        """

        return self.consensus.validate_transaction(self, transaction)

    def is_new_transaction(self, txid, exclude_block_id=None):
        """
        Return True if the transaction does not exist in any
        VALID or UNDECIDED block. Return False otherwise.

        Args:
            txid (str): Transaction ID
            exclude_block_id (str): Exclude block from search
        """
        block_statuses = self.get_blocks_status_containing_tx(txid)
        block_statuses.pop(exclude_block_id, None)
        for status in block_statuses.values():
            if status != self.BLOCK_INVALID:
                return False
        return True

    def get_block(self, block_id, include_status=False):
        """Get the block with the specified `block_id` (and optionally its status)

        Returns the block corresponding to `block_id` or None if no match is
        found.

        Args:
            block_id (str): transaction id of the transaction to get
            include_status (bool): also return the status of the block
                       the return value is then a tuple: (block, status)
        """
        block = backend.query.get_block(self.connection, block_id)
        status = None

        if include_status:
            if block:
                status = self.block_election_status(block_id,
                                                    block['block']['voters'])
            return block, status
        else:
            return block

    def get_transaction(self, txid, include_status=False):
        """Get the transaction with the specified `txid` (and optionally its status)

        This query begins by looking in the bigchain table for all blocks containing
        a transaction with the specified `txid`. If one of those blocks is valid, it
        returns the matching transaction from that block. Else if some of those
        blocks are undecided, it returns a matching transaction from one of them. If
        the transaction was found in invalid blocks only, or in no blocks, then this
        query looks for a matching transaction in the backlog table, and if it finds
        one there, it returns that.

        Args:
            txid (str): transaction id of the transaction to get
            include_status (bool): also return the status of the transaction
                                   the return value is then a tuple: (tx, status)

        Returns:
            A :class:`~.models.Transaction` instance if the transaction
            was found in a valid block, an undecided block, or the backlog table,
            otherwise ``None``.
            If :attr:`include_status` is ``True``, also returns the
            transaction's status if the transaction was found.
        """

        response, tx_status = None, None

        validity = self.get_blocks_status_containing_tx(txid)
        check_backlog = True

        if validity:
            # Disregard invalid blocks, and return if there are no valid or undecided blocks
            validity = {_id: status for _id, status in validity.items()
                        if status != Bigchain.BLOCK_INVALID}
            if validity:

                # The transaction _was_ found in an undecided or valid block,
                # so there's no need to look in the backlog table
                check_backlog = False

                tx_status = self.TX_UNDECIDED
                # If the transaction is in a valid or any undecided block, return it. Does not check
                # if transactions in undecided blocks are consistent, but selects the valid block
                # before undecided ones
                for target_block_id in validity:
                    if validity[target_block_id] == Bigchain.BLOCK_VALID:
                        tx_status = self.TX_VALID
                        break

                # Query the transaction in the target block and return
                response = backend.query.get_transaction_from_block(self.connection, txid, target_block_id)

        if check_backlog:
            response = backend.query.get_transaction_from_backlog(self.connection, txid)

            if response:
                tx_status = self.TX_IN_BACKLOG

        if response:
            response = Transaction.from_dict(response)

        if include_status:
            return response, tx_status
        else:
            return response

    def get_status(self, txid):
        """Retrieve the status of a transaction with `txid` from bigchain.

        Args:
            txid (str): transaction id of the transaction to query

        Returns:
            (string): transaction status ('valid', 'undecided',
            or 'backlog'). If no transaction with that `txid` was found it
            returns `None`
        """
        _, status = self.get_transaction(txid, include_status=True)
        return status

    def get_blocks_status_containing_tx(self, txid):
        """Retrieve block ids and statuses related to a transaction

        Transactions may occur in multiple blocks, but no more than one valid block.

        Args:
            txid (str): transaction id of the transaction to query

        Returns:
            A dict of blocks containing the transaction,
            e.g. {block_id_1: 'valid', block_id_2: 'invalid' ...}, or None
        """

        # First, get information on all blocks which contain this transaction
        blocks = backend.query.get_blocks_status_from_transaction(self.connection, txid)
        if blocks:
            # Determine the election status of each block
            validity = {
                block['id']: self.block_election_status(
                    block['id'],
                    block['block']['voters']
                ) for block in blocks
            }

            # NOTE: If there are multiple valid blocks with this transaction,
            # something has gone wrong
            if list(validity.values()).count(Bigchain.BLOCK_VALID) > 1:
                block_ids = str([block for block in validity
                                 if validity[block] == Bigchain.BLOCK_VALID])
                raise core_exceptions.CriticalDoubleInclusion(
                    'Transaction {tx} is present in '
                    'multiple valid blocks: {block_ids}'
                    .format(tx=txid, block_ids=block_ids))

            return validity

        else:
            return None

    def get_asset_by_id(self, asset_id):
        """Returns the asset associated with an asset_id.

            Args:
                asset_id (str): The asset id.

            Returns:
                dict if the asset exists else None.
        """
        cursor = backend.query.get_asset_by_id(self.connection, asset_id)
        cursor = list(cursor)
        if cursor:
            return cursor[0]['asset']

    def get_spent(self, txid, output):
        """Check if a `txid` was already used as an input.

        A transaction can be used as an input for another transaction. Bigchain needs to make sure that a
        given `txid` is only used once.

        Args:
            txid (str): The id of the transaction
            output (num): the index of the output in the respective transaction

        Returns:
            The transaction (Transaction) that used the `txid` as an input else
            `None`
        """
        # checks if an input was already spent
        # checks if the bigchain has any transaction with input {'txid': ...,
        # 'output': ...}
        transactions = list(backend.query.get_spent(self.connection, txid, output))

        # a transaction_id should have been spent at most one time
        if transactions:
            # determine if these valid transactions appear in more than one valid block
            num_valid_transactions = 0
            for transaction in transactions:
                # ignore invalid blocks
                # FIXME: Isn't there a faster solution than doing I/O again?
                if self.get_transaction(transaction['id']):
                    num_valid_transactions += 1
                if num_valid_transactions > 1:
                    raise core_exceptions.CriticalDoubleSpend(
                        '`{}` was spent more than once. There is a problem'
                        ' with the chain'.format(txid))

            if num_valid_transactions:
                return Transaction.from_dict(transactions[0])
            else:
                # all queried transactions were invalid
                return None
        else:
            return None

    def get_outputs(self, owner):
        """Retrieve a list of links to transaction outputs for a given public
           key.

        Args:
            owner (str): base58 encoded public key.

        Returns:
            :obj:`list` of TransactionLink: list of ``txid`` s and ``output`` s
            pointing to another transaction's condition
        """
        # get all transactions in which owner is in the `owners_after` list
        response = backend.query.get_owned_ids(self.connection, owner)
        links = []

        for tx in response:
            # disregard transactions from invalid blocks
            validity = self.get_blocks_status_containing_tx(tx['id'])
            if Bigchain.BLOCK_VALID not in validity.values():
                if Bigchain.BLOCK_UNDECIDED not in validity.values():
                    continue

            # NOTE: It's OK to not serialize the transaction here, as we do not
            # use it after the execution of this function.
            # a transaction can contain multiple outputs so we need to iterate over all of them
            # to get a list of outputs available to spend
            for index, output in enumerate(tx['outputs']):
                # for simple signature conditions there are no subfulfillments
                # check if the owner is in the condition `owners_after`
                if len(output['public_keys']) == 1:
                    if output['condition']['details']['public_key'] == owner:
                        links.append(TransactionLink(tx['id'], index))
                else:
                    # for transactions with multiple `public_keys` there will be several subfulfillments nested
                    # in the condition. We need to iterate the subfulfillments to make sure there is a
                    # subfulfillment for `owner`
                    if utils.condition_details_has_owner(output['condition']['details'], owner):
                        links.append(TransactionLink(tx['id'], index))
        return links

    def get_owned_ids(self, owner):
        """Retrieve a list of ``txid`` s that can be used as inputs.

        Args:
            owner (str): base58 encoded public key.

        Returns:
            :obj:`list` of TransactionLink: list of ``txid`` s and ``output`` s
            pointing to another transaction's condition
        """
        return self.get_outputs_filtered(owner, include_spent=False)

    def get_outputs_filtered(self, owner, include_spent=True):
        """
        Get a list of output links filtered on some criteria
        """
        outputs = self.get_outputs(owner)
        if not include_spent:
            outputs = [o for o in outputs
                       if not self.get_spent(o.txid, o.output)]
        return outputs

    def get_transactions_filtered(self, asset_id, operation=None):
        """
        Get a list of transactions filtered on some criteria
        """
        txids = backend.query.get_txids_filtered(self.connection, asset_id,
                                                 operation)
        for txid in txids:
            tx, status = self.get_transaction(txid, True)
            if status == self.TX_VALID:
                yield tx

    def create_block(self, validated_transactions):
        """Creates a block given a list of `validated_transactions`.

        Note that this method does not validate the transactions. Transactions
        should be validated before calling create_block.

        Args:
            validated_transactions (list(Transaction)): list of validated
                                                        transactions.

        Returns:
            Block: created block.
        """
        # Prevent the creation of empty blocks
        if not validated_transactions:
            raise exceptions.OperationError('Empty block creation is not '
                                            'allowed')

        voters = self.nodes_except_me + [self.me]
        block = Block(validated_transactions, self.me, gen_timestamp(), voters)
        block = block.sign(self.me_private)

        return block

    # TODO: check that the votings structure is correctly constructed
    def validate_block(self, block):
        """Validate a block.

        Args:
            block (Block): block to validate.

        Returns:
            The block if the block is valid else it raises and exception
            describing the reason why the block is invalid.
        """
        return self.consensus.validate_block(self, block)

    def has_previous_vote(self, block_id, voters):
        """Check for previous votes from this node

        Args:
            block_id (str): the id of the block to check
            voters (list(str)): the voters of the block to check

        Returns:
            bool: :const:`True` if this block already has a
            valid vote from this node, :const:`False` otherwise.

        Raises:
            ImproperVoteError: If there is already a vote,
                but the vote is invalid.

        """
        votes = list(backend.query.get_votes_by_block_id_and_voter(self.connection, block_id, self.me))

        if len(votes) > 1:
            raise exceptions.MultipleVotesError('Block {block_id} has {n_votes} votes from public key {me}'
                                                .format(block_id=block_id, n_votes=str(len(votes)), me=self.me))
        has_previous_vote = False
        if votes:
            if utils.verify_vote_signature(voters, votes[0]):
                has_previous_vote = True
            else:
                raise exceptions.ImproperVoteError('Block {block_id} already has an incorrectly signed vote '
                                                   'from public key {me}'.format(block_id=block_id, me=self.me))

        return has_previous_vote

    def write_block(self, block):
        """Write a block to bigchain.

        Args:
            block (Block): block to write to bigchain.
        """

        return backend.query.write_block(self.connection, block)

    def prepare_genesis_block(self):
        """Prepare a genesis block."""

        metadata = {'message': 'Hello World from the BigchainDB'}
        transaction = Transaction.create([self.me], [([self.me], 1)],
                                         metadata=metadata)

        # NOTE: The transaction model doesn't expose an API to generate a
        #       GENESIS transaction, as this is literally the only usage.
        transaction.operation = 'GENESIS'
        transaction = transaction.sign([self.me_private])

        # create the block
        return self.create_block([transaction])

    def create_genesis_block(self):
        """Create the genesis block

        Block created when bigchain is first initialized. This method is not atomic, there might be concurrency
        problems if multiple instances try to write the genesis block when the BigchainDB Federation is started,
        but it's a highly unlikely scenario.
        """

        # 1. create one transaction
        # 2. create the block with one transaction
        # 3. write the block to the bigchain

        blocks_count = backend.query.count_blocks(self.connection)

        if blocks_count:
            raise exceptions.GenesisBlockAlreadyExistsError('Cannot create the Genesis block')

        block = self.prepare_genesis_block()
        self.write_block(block)

        return block

    def vote(self, block_id, previous_block_id, decision, invalid_reason=None):
        """Create a signed vote for a block given the
        :attr:`previous_block_id` and the :attr:`decision` (valid/invalid).

        Args:
            block_id (str): The id of the block to vote on.
            previous_block_id (str): The id of the previous block.
            decision (bool): Whether the block is valid or invalid.
            invalid_reason (Optional[str]): Reason the block is invalid
        """

        if block_id == previous_block_id:
            raise exceptions.CyclicBlockchainError()

        vote = {
            'voting_for_block': block_id,
            'previous_block': previous_block_id,
            'is_block_valid': decision,
            'invalid_reason': invalid_reason,
            'timestamp': gen_timestamp()
        }

        vote_data = serialize(vote)
        signature = crypto.PrivateKey(self.me_private).sign(vote_data.encode())

        vote_signed = {
            'node_pubkey': self.me,
            'signature': signature.decode(),
            'vote': vote
        }

        return vote_signed

    def write_vote(self, vote):
        """Write the vote to the database."""
        return backend.query.write_vote(self.connection, vote)

    def get_last_voted_block(self):
        """Returns the last block that this node voted on."""

        return Block.from_dict(backend.query.get_last_voted_block(self.connection, self.me))

    def get_unvoted_blocks(self):
        """Return all the blocks that have not been voted on by this node.

        Returns:
            :obj:`list` of :obj:`dict`: a list of unvoted blocks
        """

        # XXX: should this return instaces of Block?
        return backend.query.get_unvoted_blocks(self.connection, self.me)

    def block_election_status(self, block_id, voters):
        """Tally the votes on a block, and return the status: valid, invalid, or undecided."""

        votes = list(backend.query.get_votes_by_block_id(self.connection, block_id))
        n_voters = len(voters)

        voter_counts = collections.Counter([vote['node_pubkey'] for vote in votes])
        for node in voter_counts:
            if voter_counts[node] > 1:
                raise exceptions.MultipleVotesError(
                    'Block {block_id} has multiple votes ({n_votes}) from voting node {node_id}'
                    .format(block_id=block_id, n_votes=str(voter_counts[node]), node_id=node))

        if len(votes) > n_voters:
            raise exceptions.MultipleVotesError('Block {block_id} has {n_votes} votes cast, but only {n_voters} voters'
                                                .format(block_id=block_id, n_votes=str(len(votes)),
                                                        n_voters=str(n_voters)))

        # vote_cast is the list of votes e.g. [True, True, False]
        vote_cast = [vote['vote']['is_block_valid'] for vote in votes]
        # prev_block are the ids of the nominal prev blocks e.g.
        # ['block1_id', 'block1_id', 'block2_id']
        prev_block = [vote['vote']['previous_block'] for vote in votes]
        # vote_validity checks whether a vote is valid
        # or invalid, e.g. [False, True, True]
        vote_validity = [self.consensus.verify_vote(voters, vote) for vote in votes]

        # element-wise product of stated vote and validity of vote
        # vote_cast = [True, True, False] and
        # vote_validity = [False, True, True] gives
        # [True, False]
        # Only the correctly signed votes are tallied.
        vote_list = list(compress(vote_cast, vote_validity))

        # Total the votes. Here, valid and invalid refer
        # to the vote cast, not whether the vote itself
        # is valid or invalid.
        n_valid_votes = sum(vote_list)
        n_invalid_votes = len(vote_cast) - n_valid_votes

        # The use of ceiling and floor is to account for the case of an
        # even number of voters where half the voters have voted 'invalid'
        # and half 'valid'. In this case, the block should be marked invalid
        # to avoid a tie. In the case of an odd number of voters this is not
        # relevant, since one side must be a majority.
        if n_invalid_votes >= math.ceil(n_voters / 2):
            return Bigchain.BLOCK_INVALID
        elif n_valid_votes > math.floor(n_voters / 2):
            # The block could be valid, but we still need to check if votes
            # agree on the previous block.
            #
            # First, only consider blocks with legitimate votes
            prev_block_list = list(compress(prev_block, vote_validity))
            # Next, only consider the blocks with 'yes' votes
            prev_block_valid_list = list(compress(prev_block_list, vote_list))
            counts = collections.Counter(prev_block_valid_list)
            # Make sure the majority vote agrees on previous node.
            # The majority vote must be the most common, by definition.
            # If it's not, there is no majority agreement on the previous
            # block.
            if counts.most_common()[0][1] > math.floor(n_voters / 2):
                return Bigchain.BLOCK_VALID
            else:
                return Bigchain.BLOCK_INVALID
        else:
            return Bigchain.BLOCK_UNDECIDED
