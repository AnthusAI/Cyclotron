"""Specs for the application-facing library imports."""


def test_applications_can_import_the_reusable_flywheel_from_the_package():
    from decision_flywheel import DecisionFlywheel, FittedClassifier
    from decision_flywheel.flywheel import DecisionFlywheel as Implementation
    assert DecisionFlywheel is Implementation
    assert FittedClassifier.__name__ == "FittedClassifier"
