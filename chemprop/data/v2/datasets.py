from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

import numpy as np
from rdkit import Chem
from torch.utils.data import Dataset

from chemprop.data.scaler import StandardScaler
from chemprop.featurizers.v2 import MolGraph, MoleculeFeaturizer
from chemprop.data.v2.datapoints import DatapointBase, MoleculeDatapoint, ReactionDatapoint
from chemprop.featurizers.v2.reaction import ReactionFeaturizer


class MolGraphDataset(Dataset, ABC):
    def __init__(self, data: Sequence[DatapointBase]):
        if data is None:
            raise ValueError("arg: `data` was None!")

        self.data = data

    def __len__(self) -> int:
        return len(self.data)

    @property
    def targets(self) -> np.ndarray:
        return np.array([d.targets for d in self.data])

    @targets.setter
    def targets(self, targets: np.ndarray) -> None:
        if not len(self.data) == len(targets):
            raise ValueError(
                "number of molecules and targets must be of same length! "
                f"num molecules: {len(self.data)}, num targets: {len(targets)}"
            )

        for i in range(len(self.data)):
            self.data[i].targets = targets[i]

    @property
    def features(self) -> np.ndarray:
        if len(self.data) > 0 and self.data[0].features is None:
            return None

        return np.array([d.features for d in self.data])

    @property
    def phase_features(self) -> list[np.ndarray]:
        if len(self.data) > 0 and self.data[0].phase_features is None:
            return None

        return [d.phase_features for d in self.data]

    @property
    def data_weights(self) -> np.ndarray:
        return np.array([d.data_weight for d in self.data])

    @property
    def gt_targets(self) -> np.ndarray:
        if len(self.data) > 0 and self.data[0].gt_targets is None:
            return None

        return np.array([d.gt_targets for d in self.data])

    @property
    def lt_targets(self) -> list[np.ndarray]:
        if len(self.data) > 0 and self.data[0].lt_targets is None:
            return None

        return np.array([d.lt_targets for d in self.data])

    @property
    def num_tasks(self) -> Optional[int]:
        return self.data[0].num_tasks if len(self.data) > 0 else None

    @property
    def features_size(self) -> Optional[int]:
        if len(self.data) > 0 and self.data[0].features is None:
            return None

        return len(self.data[0].features)

    def normalize_targets(self) -> StandardScaler:
        """Normalizes the targets of the dataset using a `StandardScaler`

        The StandardScaler subtracts the mean and divides by the standard deviation for each task
        independently. NOTE: This should only be used for regression datasets.

        Returns
        -------
        StandardScaler
            a scaler fit to the targets.
        """
        targets = [d.raw_targets for d in self.data]
        scaler = StandardScaler().fit(targets)
        scaled_targets = scaler.transform(targets)
        self.targets = scaled_targets

        return scaler

    def reset_features_and_targets(self) -> None:
        """Resets the features (atom, bond, and molecule) and targets to their raw values."""
        for d in self.data:
            d.reset_features_and_targets()


class MoleculeDataset(MolGraphDataset):
    """A `MoleculeDataset` contains a list of `MoleculeDatapoint`s with access to
    their attributes.

    Parameters
    ----------
    data : Sequence[MoleculeDatapoint]
        the dataset from which to load
    featurizer : MoleculeFeaturizer
        the featurizer with which to generate MolGraphs of the input
    """

    def __init__(self, data: Sequence[MoleculeDatapoint], featurizer: MoleculeFeaturizer):
        super().__init__(data)
        self.featurizer = featurizer

    def __getitem__(self, idx: int) -> tuple[MolGraph, np.ndarray]:
        d = self.data[idx]

        return self.featurizer(d.mol, d.atom_features, d.bond_features), d.targets

    @property
    def smiles(self) -> list[str]:
        return [d.smi for d in self.data]

    @property
    def mols(self) -> list[Chem.Mol]:
        return [d.mol for d in self.data]

    @property
    def number_of_molecules(self) -> int:
        return 1

    @property
    def atom_features(self) -> list[np.ndarray]:
        if len(self.data) > 0 and self.data[0].atom_features is None:
            return None

        return [d.atom_features for d in self.data]

    @property
    def bond_features(self) -> list[np.ndarray]:
        if len(self.data) > 0 and self.data[0].bond_features is None:
            return None

        return [d.bond_features for d in self.data]

    @property
    def atom_features_size(self) -> int:
        if len(self.data) > 0 and self.data[0].atom_features is None:
            return None

        return len(self.data[0].atom_features[0])

    @property
    def bond_features_size(self) -> int:
        if len(self.data) > 0 and self.data[0].bond_features is None:
            return None

        return len(self.data[0].bond_features[0])

    def normalize_features(
        self,
        scaler: StandardScaler = None,
        replace_nan_token: int = 0,
        scale_atom_descriptors: bool = False,
        scale_bond_features: bool = False,
    ) -> StandardScaler:
        """Normalizes the features of the dataset using a :class:`~chemprop.data.StandardScaler`.

        The :class:`~chemprop.data.StandardScaler` subtracts the mean and divides by the standard
        deviation for each feature independently.

        If a :class:`~chemprop.data.StandardScaler` is provided, it is used to perform the
        normalization. Otherwise, a :class:`~chemprop.data.StandardScaler` is first fit to the
        features in this  dataset and is then used to perform the normalization.

        :param scaler: A fitted :class:`~chemprop.data.StandardScaler`. If it is provided it is
            used, otherwise a new :class:`~chemprop.data.StandardScaler` is first fitted to this
            data and is then used.
        :param replace_nan_token: A token to use to replace NaN entries in the features.
        :param scale_atom_descriptors: If the features that need to be scaled are atom features
            rather than molecule.
        :param scale_bond_features: If the features that need to be scaled are bond descriptors
            rather than molecule.
        :return: A fitted :class:`~chemprop.data.StandardScaler`. If a :class:`~chemprop.data.
            StandardScaler` is provided as a parameter, this is the same :class:`~chemprop.data.
            StandardScaler`. Otherwise, this is a new :class:`~chemprop.data.StandardScaler` that
            has been fit on this dataset.
        """
        if len(self.data) == 0 or (
            self.data[0].features is None and not scale_bond_features and not scale_atom_descriptors
        ):
            return None

        if scaler is None:
            if scale_atom_descriptors and not self.data[0].atom_descriptors is None:
                features = np.vstack([d.raw_atom_descriptors for d in self.data])
            elif scale_atom_descriptors and not self.data[0].atom_features is None:
                features = np.vstack([d.raw_atom_features for d in self.data])
            elif scale_bond_features:
                features = np.vstack([d.raw_bond_features for d in self.data])
            else:
                features = np.vstack([d.raw_features for d in self.data])

            scaler = StandardScaler(replace_nan_token=replace_nan_token)
            scaler.fit(features)

        if scale_atom_descriptors and not self.data[0].atom_descriptors is None:
            for d in self.data:
                d.atom_descriptors = scaler.transform(d.raw_atom_descriptors)
        elif scale_atom_descriptors and not self.data[0].atom_features is None:
            for d in self.data:
                d.atom_features = scaler.transform(d.raw_atom_features)
        elif scale_bond_features:
            for d in self.data:
                d.bond_features = scaler.transform(d.raw_bond_features)
        else:
            for d in self.data:
                d.features = scaler.transform(d.raw_features.reshape(1, -1))[0]

        return scaler


class ReactionDataset(MolGraphDataset):
    """A `ReactionDataset` contains a list of `ReactionDatapoint`s with access to
    their attributes.

    Parameters
    ----------
    data : Sequence[ReactionDatapoint]
        the dataset from which to load
    featurizer : ReactionFeaturizer
        the featurizer with which to generate MolGraphs of the input
    """

    def __init__(self, data: Sequence[ReactionDatapoint], featurizer: ReactionFeaturizer):
        super().__init__(data)
        self.featurizer = featurizer

    def __getitem__(self, idx: int) -> tuple[MolGraph, np.ndarray]:
        d = self.data[idx]

        return self.featurizer(d.mols, d.atom_features, d.bond_features), d.targets

    @property
    def smiles(self) -> list[str]:
        return [d.smis for d in self.data]

    @property
    def mols(self) -> list[Chem.Mol]:
        return [d.mols for d in self.data]

    @property
    def number_of_molecules(self) -> int:
        return self.data[0].number_of_molecules