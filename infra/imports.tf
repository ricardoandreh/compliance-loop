# One-time adoption of resources that existed before OpenTofu managed them.
#
# Lambda creates its log group on first invocation, with retention "never
# expire". Declaring the groups (to set a 30-day retention) would fail on the
# existing ones, so they are imported instead. After the first apply these
# blocks are no-ops and can be removed; on a fresh account they are skipped
# because OpenTofu creates the groups before the functions exist.
import {
  to = module.analyze.aws_cloudwatch_log_group.lambda
  id = "/aws/lambda/compliance-loop-analyze-dev"
}

import {
  to = module.execute.aws_cloudwatch_log_group.lambda
  id = "/aws/lambda/compliance-loop-execute-dev"
}

import {
  to = module.escalation_lambda.aws_cloudwatch_log_group.lambda
  id = "/aws/lambda/compliance-loop-escalation-dev"
}

import {
  to = module.controls_sync.aws_cloudwatch_log_group.lambda
  id = "/aws/lambda/compliance-loop-controls-sync-dev"
}

import {
  to = module.exception_scheduler.aws_cloudwatch_log_group.lambda
  id = "/aws/lambda/compliance-loop-exception-scheduler-sync-dev"
}
