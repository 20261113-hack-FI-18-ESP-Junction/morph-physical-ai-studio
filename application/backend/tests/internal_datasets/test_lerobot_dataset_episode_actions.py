from types import SimpleNamespace

import pytest
import torch
from datasets import Dataset
from pydantic import ValidationError

from internal_datasets.lerobot.lerobot_dataset import InternalLeRobotDataset


def episode_from_actions(
    actions: list, shape: tuple[int, ...], start: int = 0, stop: int | None = None, *, tensor_sequence: bool = False
):
    client = InternalLeRobotDataset.__new__(InternalLeRobotDataset)
    names = [f"joint_{index}.pos" for index in range(shape[0])]
    client._dataset = SimpleNamespace(
        features={"action": {"shape": shape, "names": names}},
        hf_dataset=Dataset.from_dict({"action": actions}).with_format("torch"),
        meta=SimpleNamespace(fps=10, video_keys=[]),
    )
    if tensor_sequence:
        client._dataset.hf_dataset = {"action": [torch.tensor(value) for value in actions]}
    stop = len(actions) if stop is None else stop
    return client._build_episode_from_metadata(
        {
            "episode_index": 0,
            "dataset_from_index": start,
            "dataset_to_index": stop,
            "length": stop - start,
            "tasks": ["open gripper"],
        }
    )


@pytest.mark.parametrize("tensor_sequence", [False, True])
def test_scalar_action_column_keeps_one_value_per_frame_and_episode_slice(tensor_sequence: bool) -> None:
    # LeRobot's shape-(1,) Parquet column is returned as one scalar tensor per frame.
    episode = episode_from_actions(
        [1999.0, 2018.0, 2034.0, 2018.0, 1999.0], (1,), 1, 4, tensor_sequence=tensor_sequence
    )

    assert episode.actions == [[2018.0], [2034.0], [2018.0]]
    assert episode.action_keys == ["joint_0.pos"]
    assert episode.length == 3


@pytest.mark.parametrize("tensor_sequence", [False, True])
def test_single_frame_scalar_action_keeps_both_axes(tensor_sequence: bool) -> None:
    episode = episode_from_actions([2018.0], (1,), tensor_sequence=tensor_sequence)

    assert episode.actions == [[2018.0]]


@pytest.mark.parametrize(
    ("actions", "shape"),
    [([[2018.0], [2034.0]], (1,)), ([[1.0, 2.0], [3.0, 4.0]], (2,))],
)
@pytest.mark.parametrize("tensor_sequence", [False, True])
def test_vector_action_columns_are_unchanged(actions: list, shape: tuple[int, ...], tensor_sequence: bool) -> None:
    episode = episode_from_actions(actions, shape, tensor_sequence=tensor_sequence)

    assert episode.actions == actions


@pytest.mark.parametrize("tensor_sequence", [False, True])
def test_scalar_column_declared_as_multiple_actions_still_fails_validation(tensor_sequence: bool) -> None:
    with pytest.raises(ValidationError):
        episode_from_actions([2018.0, 2034.0], (2,), tensor_sequence=tensor_sequence)


@pytest.mark.parametrize("tensor_sequence", [False, True])
def test_extra_action_dimensions_still_fail_validation(tensor_sequence: bool) -> None:
    with pytest.raises(ValidationError):
        episode_from_actions([[[1.0, 2.0]], [[3.0, 4.0]]], (2,), tensor_sequence=tensor_sequence)
