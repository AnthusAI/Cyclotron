Feature: Use the decision flywheel without a web application
  The reusable library must work without FastAPI, GraphQL, SQLite workspace
  screens, or React. An application supplies a decision-model adapter and an
  optimizer adapter, then observes structured events.

  Scenario: A headless application predicts an item with an injected model
    Given a headless flywheel with a scripted decision model
    When the application predicts a new item
    Then the application receives the predicted label and confidence
    And the application can inspect a prediction event

  Scenario: A core module does not depend on the web application
    Given the reusable core module list
    When its imports are inspected
    Then no core module imports a web, trace, or reviewer module
