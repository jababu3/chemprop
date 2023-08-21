from __future__ import annotations

from abc import abstractmethod

import torch
from torch import Tensor, nn

from chemprop.v2.conf import DEFAULT_ATOM_FDIM, DEFAULT_BOND_FDIM, DEFAULT_HIDDEN_DIM
from chemprop.v2.exceptions import InvalidShapeError
from chemprop.v2.featurizers import BatchMolGraph
from chemprop.v2.models.utils import get_activation_function
from chemprop.v2.models.modules.message_passing.base import MessagePassingBlock


class MessagePassingBlockBase(MessagePassingBlock):
    """The base message-passing block for atom- and bond-based MPNNs

    NOTE: this class is an abstract base class and cannot be instantiated

    Parameters
    ----------
    d_v : int, default=DEFAULT_ATOM_FDIM
        the feature dimension of the vertices
    d_e : int, default=DEFAULT_BOND_FDIM
        the feature dimension of the edges
    d_h : int, default=DEFAULT_HIDDEN_DIM
        the hidden dimension during message passing
    bias : bool, defuault=False
        if `True`, add a bias term to the learned weight matrices
    depth : int, default=3
        the number of message passing iterations
    undirected : bool, default=False
        if `True`, pass messages on undirected edges
    dropout : float, default=0
        the dropout probability
    activation : str, default="relu"
        the activation function to use
    aggregation : Aggregation | None, default=None
        the aggregation operation to use during molecule-level readout. If `None`, use `MeanAggregation`
    d_vd : int | None, default=None
        the dimension of additional vertex descriptors that will be concatenated to the hidden features before readout

    See also
    --------
    `AtomMessageBlock`

    `BondMessageBlock`
    """

    def __init__(
        self,
        d_v: int = DEFAULT_ATOM_FDIM,
        d_e: int = DEFAULT_BOND_FDIM,
        d_h: int = DEFAULT_HIDDEN_DIM,
        bias: bool = False,
        depth: int = 3,
        undirected: bool = False,
        dropout: float = 0,
        activation: str = "relu",
        d_vd: int | None = None,
        # layers_per_message: int = 1,
    ):
        super().__init__()

        self.depth = depth
        self.undirected = undirected
        # self.layers_per_message = 1

        self.dropout = nn.Dropout(dropout)
        self.tau = get_activation_function(activation)

        self.__output_dim = d_h

        if d_vd is not None:
            self.d_vd = d_vd
            self.__output_dim += d_vd
            self.W_vd = nn.Linear(d_h + d_vd, d_h + d_vd)

        self.W_i, self.W_h, self.W_o = self.setup_weight_matrices(d_v, d_e, d_h, bias)

    @abstractmethod
    def setup_weight_matrices(
        self, d_v: int, d_e: int, d_h: int = 300, bias: bool = False
    ) -> tuple[nn.Module, nn.Module, nn.Module]:
        """set up the weight matrices used in the message passing udpate functions

        Parameters
        ----------
        d_v : int
            the vertex feature dimension
        d_e : int
            the edge feature dimension
        d_h : int, default=300
            the hidden dimension during message passing
        bias: bool, deafault=False
            whether to add a learned bias to the matrices

        Returns
        -------
        tuple[nn.Module, nn.Module, nn.Module]
            the input, hidden, and output weight matrices, respectively, used in the message
            passing update functions
        """

    @property
    def output_dim(self) -> int:
        return self.W_o.out_features

    def finalize(self, M_v: Tensor, V: Tensor, V_d: Tensor | None) -> Tensor:
        r"""Finalize message passing by (1) concatenating the final hidden representations `H_v` and the original vertex `V` and (2) further concatenating additional vertex descriptors `V_d`, if provided.

        This function implements the following operation:

        .. math::
            H_v &= \mathtt{dropout} \left( \tau(W_o([V, M_v])) \right) \\
            H_v &= \mathtt{dropout} \left( \tau(W_vd([H_v, V_d])) \right),

        where :math:`\tau` is the activation function, :math:`W_o` and :math:`W_vd` are learned
        weight matrices, :math:`M_v` is the learned message matrix, :math:`V` is the original
        vertex feature matrix, and :math:`V_d` is an optional vertex descriptor matrix.

        Parameters
        ----------
        M_v : Tensor
            a tensor of shape `V x d_h` containing the messages sent from each atom
        V : Tensor
            a tensor of shape `V x d_v` containing the original vertex features
        V_d : Tensor | None
            an optional tensor of shape `V x d_vd` containing additional vertex descriptors

        Returns
        -------
        Tensor
            a tensor of shape `V x (d_h + d_v [+ d_vd])` containing the final hidden representations

        Raises
        ------
        InvalidShapeError
            if `V_d` is not of shape `b x d_vd`, where `b` is the batch size and `d_vd` is the
            vertex descriptor dimension
        """
        H_v = self.W_o(torch.cat((V, M_v), 1))  # V x d_h
        H_v = self.tau(H_v)
        H_v = self.dropout(H_v)

        if V_d is not None:
            try:
                H_vd = torch.cat((H_v, V_d), 1)
                H_v = self.W_vd(H_vd)
                H_v = self.dropout(H_v)
            except RuntimeError:
                raise InvalidShapeError("V_d", V_d.shape, [len(H_v), self.d_vd])

        return H_v

    @abstractmethod
    def forward(self, bmg: BatchMolGraph, V_d: Tensor | None = None) -> Tensor:
        """Encode a batch of molecular graphs.

        Parameters
        ----------
        bmg: BatchMolGraph
            a batch of `b` :obj:`~chemprop.v2.featurizers.MolGraph`s to encode
        V_d : Tensor | None, default=None
            an optional tensor of shape `V x d_vd` containing additional descriptors for each atom
            in the batch. These will be concatenated to the learned atomic descriptors and
            transformed before the readout phase. NOTE: recall that `V` is equal to `num_atoms + 1`,
            so if provided, this tensor must be 0-padded in the 0th row.

        Returns
        -------
        Tensor
            a tensor of shape `b x d_h` or `b x (d_h + d_vd)` containing the encoding of each
            molecule in the batch, depending on whether additional atom descriptors were provided
        """


class BondMessageBlock(MessagePassingBlockBase):
    def setup_weight_matrices(self, d_v: int, d_e: int, d_h: int = 300, bias: bool = False):
        W_i = nn.Linear(d_e, d_h, bias)
        W_h = nn.Linear(d_h, d_h, bias)
        W_o = nn.Linear(d_v + d_h, d_h)

        return W_i, W_h, W_o

    def forward(self, bmg: BatchMolGraph, V_d: Tensor | None = None) -> Tensor:
        H_0 = self.W_i(bmg.E)
        H_e = self.tau(H_0)

        for _ in range(1, self.depth):
            if self.undirected:
                H_e = (H_e + H_e[bmg.b2revb]) / 2

            # MESSAGE
            M_e_k = H_e[bmg.a2b]  # E x n_bonds x d_h
            M_e = M_e_k.sum(1)[bmg.b2a]  # E x d_h
            M_e = M_e - H_e[bmg.b2revb]  # subtract reverse bond message

            # UPDATE
            H_e = self.W_h(M_e)  # E x d_h
            H_e = self.tau(H_0 + H_e)
            H_e = self.dropout(H_e)

        M_v_k = H_e[bmg.a2b]
        M_v = M_v_k.sum(1)  # V x d_h

        return self.finalize(M_v, bmg.V, V_d)


class AtomMessageBlock(MessagePassingBlockBase):
    def setup_weight_matrices(self, d_v: int, d_e: int, d_h: int = 300, bias: bool = False):
        W_i = nn.Linear(d_v, d_h, bias)
        W_h = nn.Linear(d_e + d_h, d_h, bias)
        W_o = nn.Linear(d_v + d_h, d_h)

        return W_i, W_h, W_o

    def forward(self, bmg: BatchMolGraph, V_d: Tensor | None = None) -> Tensor:
        H_0 = self.W_i(bmg.V)  # V x d_h
        H_v = self.tau(H_0)

        for _ in range(1, self.depth):
            if self.undirected:
                H_v = (H_v + H_v[bmg.b2revb]) / 2

            # aggregate messages
            M_v_k = torch.cat((H_v[bmg.a2a], bmg.E[bmg.a2b]), 2)  # V x b x (d_h + d_e)
            M_v = M_v_k.sum(1)  # V x d_h + d_e

            # UPDATE
            H_v = self.W_h(M_v)  # E x d_h
            H_v = self.tau(H_0 + H_v)
            H_v = self.dropout(H_v)

        M_v_k = H_v[bmg.a2a]
        M_v = M_v_k.sum(1)  # V x d_h

        return self.finalize(M_v, bmg.V, V_d)
