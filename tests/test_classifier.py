from app.classifiers.deterministic import DeterministicClassifier


def test_deterministic_classifier_maps_keywords():
    classifier = DeterministicClassifier()
    result = classifier.classify("This is unlicensed copyright footage from a film.")
    assert result.signal_type == "copyright"
    assert result.confidence >= 0.55
    assert result.model_version == "deterministic-keywords-v1"


def test_deterministic_classifier_falls_back_to_other():
    result = DeterministicClassifier().classify("I disagree with the editorial choice.")
    assert result.signal_type == "other"
    assert result.confidence == 0.3
