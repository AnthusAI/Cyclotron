Feature: Host the flywheel from an application runtime
  The API command worker must not choose a dataset adapter, provider adapter,
  or classifier vocabulary. An injected workspace runtime owns that work.

  Scenario: A worker delegates a new run to an injected runtime
    Given a web worker with a scripted workspace runtime
    When the application creates a run
    Then the runtime receives the caller configuration and items
    And the saved run contains the runtime-normalized configuration
