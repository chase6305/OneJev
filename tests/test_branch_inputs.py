"""Unequal branches must keep their tokens, attention masks and final readout positions."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qev.engine import DecisionEngine
from qev.mm_engine import MMDecisionEngine, Prepared


@pytest.mark.parametrize("multimodal", [False, True], ids=["text", "multimodal"])
def test_chunk_input_padding_masks_and_readouts_follow_the_selected_branches(multimodal):
    torch = pytest.importorskip("torch")
    engine = object.__new__(MMDecisionEngine if multimodal else DecisionEngine)
    engine.device, engine.pad_id = torch.device("cpu"), 7
    observed = {}

    def decoder(input_ids, attention_mask, position_ids, **kwargs):
        observed.update(ids=input_ids.clone(), mask=attention_mask.clone(), positions=position_ids.clone())
        pos = position_ids[0] if multimodal else position_ids
        return SimpleNamespace(last_hidden_state=torch.stack([input_ids, pos], dim=-1).float())

    engine.model = SimpleNamespace(model=decoder)
    engine._fork = Mock(return_value="independent cache")
    engine._readout_rows = lambda hidden, counts: [(row[:count].tolist(), .5) for row, count in zip(hidden, counts)]
    suffixes = [[11, 12], [21], [31, 32, 33], [41, 42, 43, 44]]
    rendered = [SimpleNamespace(qid=f"q{i}", kind="choice", labels=["a", "b"],
                                descriptions=["first", "second"], n_slots=2) for i in range(4)]
    results = [None] * 4
    if multimodal:
        engine._run_chunk_mm("base", 5, -2, [2, 1, 0], rendered, suffixes, results)
    else:
        engine._run_chunk("base", 5, [2, 1, 0], rendered, suffixes, results)
    assert observed["ids"].tolist() == [[31, 32, 33], [21, 7, 7], [11, 12, 7]]
    assert observed["mask"].dtype == torch.long
    assert observed["mask"].tolist() == [[1] * 8, [1] * 6 + [0, 0], [1] * 7 + [0]]
    if multimodal:
        assert observed["positions"].tolist() == [[[3, 4, 5]] * 3] * 3
        assert [r.logits for r in results[:3]] == [[12., 4.], [21., 3.], [33., 5.]]
    else:
        assert observed["positions"].tolist() == [[5, 6, 7], [5, 0, 0], [5, 6, 0]]
        assert [r.logits for r in results[:3]] == [[12., 6.], [21., 5.], [33., 7.]]
    assert results[3] is None
    assert [r.suffix_tokens for r in results[:3]] == [2, 1, 3]
    engine._fork.assert_called_once_with("base", 3)


def test_multimodal_chunk_budget_includes_the_longest_question():
    torch = pytest.importorskip("torch")
    engine = object.__new__(MMDecisionEngine)
    engine.device = torch.device("cpu")
    engine.fork_token_budget = 16
    engine._fork_graphs = None
    prep = Prepared(key=(), prefix_ids=[1, 2], suffixes=[[3] * n for n in (6, 1, 4, 2, 5)],
                    inputs={}, positions=None, delta=0)
    engine._prepare = lambda state, rendered: prep
    engine._prefill = Mock(return_value="base")
    chunks = []

    def run_chunk(base, prefix, delta, idx, rendered, suffixes, results):
        chunks.append(idx)
        assert len(idx) * (prefix + max(len(suffixes[i]) for i in idx)) <= engine.fork_token_budget
        for i in idx:
            results[i] = f"result-{i}"

    engine._run_chunk_mm = run_chunk
    assert engine._run_batched("state", [object()] * 5) == [f"result-{i}" for i in range(5)]
    assert chunks == [[0, 1], [2, 3], [4]]
    engine._prefill.assert_called_once_with(prep)


@pytest.mark.parametrize("sliding", [False, True])
def test_text_batched_padding_matches_independent_full_prompt_logits(sliding):
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        torch.manual_seed(19)
        config = transformers.Qwen3Config(
            vocab_size=64, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
            num_attention_heads=4, num_key_value_heads=2, head_dim=8, max_position_embeddings=128,
            layer_types=["full_attention", "sliding_attention" if sliding else "full_attention"],
            sliding_window=3 if sliding else None, use_sliding_window=sliding,
        )
        engine = object.__new__(DecisionEngine)
        engine.model = transformers.Qwen3ForCausalLM(config).eval().requires_grad_(False)
        engine.device, engine.pad_id = torch.device("cpu"), 0
        engine.head_dtype, engine.slot_ids, engine._want_mass = "float32", [10, 11], False
        engine._graphs, engine.fork_token_budget = None, 128
        engine._init_slot_head()
        prefix, suffixes = [1, 2, 3, 4], [[5, 6, 7], [8], [9, 10]]
        engine._prefix_and_suffix_ids = lambda state, rendered: (prefix, suffixes)
        rendered = [SimpleNamespace(qid=f"q{i}", kind="choice", labels=["a", "b"],
                                    descriptions=["first", "second"], n_slots=2) for i in range(3)]
        with torch.inference_mode():
            expected = []
            for suffix in suffixes:
                out = engine.model.model(input_ids=torch.tensor([prefix + suffix]), use_cache=False, return_dict=True)
                expected.append(engine._readout_rows(out.last_hidden_state[:, -1], [2])[0][0])
            actual = engine._run_batched("state", rendered)
        torch.testing.assert_close(torch.tensor([r.logits for r in actual]), torch.tensor(expected), atol=2e-6, rtol=2e-5)
    finally:
        torch.set_num_threads(previous)
