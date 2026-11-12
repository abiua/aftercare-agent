from aftercare.evaluation.campaign_metrics import protocol_valid


def test_renaming_arm_does_not_prove_counterexample_ablation():
    row={'arm':'multi_synthesis_only','candidate_present':True}
    unchanged=[{'kind':'investigation_route','payload':{}}, {'kind':'investigation_input','payload':{'role':'verifier'}}]
    assert not protocol_valid(row,unchanged)
    actual=[{'kind':'investigation_route','payload':{'counterexample_ablation':True}}, {'kind':'investigation_input','payload':{'role':'synthesizer'}}]
    assert protocol_valid(row,actual)
    assert not protocol_valid(row,actual+unchanged)
