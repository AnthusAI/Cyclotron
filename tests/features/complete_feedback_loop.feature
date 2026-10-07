Feature: Complete an observable feedback loop
  A host application can use the reusable core to turn trusted feedback into
  an optimizer proposal, decision-model features, a fitted decision head, and
  a durable version. The same records must make corrections visible instead of
  silently serving a head that learned from superseded feedback.

  Scenario: Trusted feedback produces an observable fitted classifier
    Given a reusable flywheel with scripted feedback and trusted labels
    When the application improves the classifier
    Then the optimizer proposal, decision requests, and fitted head are observable
    And a new prediction is served by the fitted head
    And reopening the flywheel preserves the fitted version without a new model call

  Scenario: A corrected label invalidates the dependent fitted classifier
    Given a reusable flywheel with scripted feedback and trusted labels
    When the application improves the classifier
    And a trusted label is corrected
    Then the fitted classifier is invalidated and the correction is observable
