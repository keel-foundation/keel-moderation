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


def test_linear_classifier_is_a_trained_model():
    from app.classifiers.linear import LinearClassifier

    classifier = LinearClassifier()
    assert classifier.model_version.startswith("linear-nb-v1+")
    assert "spam" in classifier.labels
    assert "copyright" in classifier.labels


def test_linear_classifier_separates_signal_types():
    from app.classifiers.linear import LinearClassifier

    classifier = LinearClassifier()
    spam = classifier.classify(
        "Buy cheap followers and boost your views with our bot network."
    )
    assert spam.signal_type == "spam"
    piracy = classifier.classify("This is the full movie ripped from a streaming service.")
    assert piracy.signal_type == "piracy"
    copyright_case = classifier.classify(
        "The creator copied a documentary sequence frame for frame."
    )
    assert copyright_case.signal_type == "copyright"
    impersonation = classifier.classify("This account is impersonating a famous creator.")
    assert impersonation.signal_type == "impersonation"
    neutral = classifier.classify("The audio is a little quiet in this episode.")
    assert neutral.signal_type == "other"


def test_classifier_backend_selection(monkeypatch):
    from app.classifiers.deterministic import DeterministicClassifier
    from app.classifiers.http import classifier_from_env
    from app.classifiers.linear import LinearClassifier

    monkeypatch.setenv("KEELTUBE_CLASSIFIER_BACKEND", "deterministic")
    assert isinstance(classifier_from_env(), DeterministicClassifier)

    monkeypatch.setenv("KEELTUBE_CLASSIFIER_BACKEND", "linear")
    assert isinstance(classifier_from_env(), LinearClassifier)

    monkeypatch.setenv("KEELTUBE_CLASSIFIER_BACKEND", "transformers")
    assert classifier_from_env() is not None
