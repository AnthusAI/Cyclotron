Feature: Label items in the Cyclotron workspace
  The browser application renders API-owned state. It does not implement
  flywheel fitting or optimization rules in the client.

  Scenario: A labeler can compare a displayed prediction with a human label
    Given a scorecard item and its model predictions from the API
    When the labeler chooses a classifier label and submits feedback
    Then the interface shows that feedback is being recorded
    And the next rendered state comes from the API

  Scenario: The browser does not own optimization logic
    Given the frontend source tree
    When its application imports are inspected
    Then it contains no decision-provider credentials or model-fitting implementation
