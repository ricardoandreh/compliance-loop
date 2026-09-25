# The loop's own health, as alarms on the failures that would otherwise be
# silent. Each one answers "which part of the loop stopped, without anything
# else noticing?" — see design.md, "Operação: alarmes e runbook".
#
# Escalations are NOT alarmed here: a FAILED execution with a GitHub issue is the
# loop working as designed (C4, C8, C9, CTRL-06). These alarms are about the
# loop not working at all.

variable "project_name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "alert_topic_arn" { type = string }
variable "rule_name" { type = string }
variable "state_machine_arn" { type = string }
variable "schedule_group" { type = string }
variable "stream_dlq_name" { type = string }
variable "intake_dlq_name" { type = string }
variable "alarm_queue_name" { type = string }
variable "escalation_dlq_name" { type = string }
variable "exception_sync_log_group" { type = string }
variable "function_names" {
  description = "Role in the loop -> Lambda function name."
  type        = map(string)
}

locals {
  p       = "${var.project_name}-${var.environment}"
  actions = [var.alert_topic_arn]
}

# --- Intake: detections that never started an execution -------------------
# EventBridge matched a CloudTrail event but could not start the state machine
# (role trust broken, state machine deleted, throttled for an hour). The
# violation happened and the loop has not seen it. FailedInvocations alone
# reports it for one period and returns to OK with the event still unhandled;
# the depth of the intake queue holds the alarm until the queue is replayed or
# emptied. (Measured in the fault injection: each refusal counted both as a
# FailedInvocation and as InvocationsSentToDlq.) InvocationsFailedToBeSentToDlq
# covers the one case the queue cannot hold: the queue itself refused it.
resource "aws_cloudwatch_metric_alarm" "intake_failed" {
  alarm_name          = "${local.p}-intake-failed"
  alarm_description   = "A detection did not start an execution. Waiting in the intake queue: fix the cause, then scripts/replay_intake.py. Runbook: design.md, Operação."
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions

  metric_query {
    id          = "undelivered"
    expression  = "FILL(waiting, 0) + FILL(failed, 0) + FILL(unqueued, 0)"
    label       = "Detections that did not start an execution"
    return_data = true
  }

  metric_query {
    id = "waiting"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = var.intake_dlq_name }
      stat        = "Maximum"
      period      = 300
    }
  }

  metric_query {
    id = "failed"
    metric {
      namespace   = "AWS/Events"
      metric_name = "FailedInvocations"
      dimensions  = { RuleName = var.rule_name }
      stat        = "Sum"
      period      = 300
    }
  }

  metric_query {
    id = "unqueued"
    metric {
      namespace   = "AWS/Events"
      metric_name = "InvocationsFailedToBeSentToDlq"
      dimensions  = { RuleName = var.rule_name }
      stat        = "Sum"
      period      = 300
    }
  }
}

# --- Exceptions: expiries that will not be re-evaluated ---------------------
resource "aws_cloudwatch_metric_alarm" "scheduler_target_errors" {
  alarm_name          = "${local.p}-scheduler-target-errors"
  alarm_description   = "A schedule fired but could not start the state machine: an exception expired without re-evaluation."
  namespace           = "AWS/Scheduler"
  metric_name         = "TargetErrorCount"
  dimensions          = { ScheduleGroup = var.schedule_group }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dropped" {
  alarm_name          = "${local.p}-scheduler-dropped"
  alarm_description   = "Scheduler dropped an invocation after its retries: an exception expired without re-evaluation."
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationDroppedCount"
  dimensions          = { ScheduleGroup = var.schedule_group }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# The sync Lambda catches its per-record errors and reports them as batch item
# failures, so its Errors metric stays at zero while schedules are not being
# created. This is the failure C3 exposed (Scheduler rejecting the role's trust
# policy): count it from the log line instead.
resource "aws_cloudwatch_log_metric_filter" "exception_sync_failures" {
  name           = "${local.p}-exception-sync-failures"
  log_group_name = var.exception_sync_log_group
  pattern        = "\"Failed to process\""
  metric_transformation {
    name          = "ExceptionSyncRecordFailures"
    namespace     = "ComplianceLoop"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "exception_sync_failures" {
  alarm_name          = "${local.p}-exception-sync-failures"
  alarm_description   = "An exception change could not be synchronised with the Scheduler (it will be retried, then sent to the stream DLQ)."
  namespace           = "ComplianceLoop"
  metric_name         = aws_cloudwatch_log_metric_filter.exception_sync_failures.metric_transformation[0].name
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# --- Records given up on ----------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "stream_dlq" {
  alarm_name          = "${local.p}-stream-dlq"
  alarm_description   = "A stream record (control or exception change) was abandoned after retries. Replay it from the queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.stream_dlq_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# --- Crashes (not escalations) -----------------------------------------------
# Analyze and Execute return structured errors, so Errors counts only what they
# could not report: timeouts, out-of-memory, runtime crashes.
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = var.function_names
  alarm_name          = "${local.p}-${each.key}-errors"
  alarm_description   = "Lambda ${each.value} crashed or timed out (structured failures are escalated, not counted here)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "sfn_timed_out" {
  alarm_name          = "${local.p}-executions-timed-out"
  alarm_description   = "An execution hit the 600 s global timeout: a control's worst case no longer fits the budget (Property 28)."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  dimensions          = { StateMachineArn = var.state_machine_arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "sfn_throttled" {
  alarm_name          = "${local.p}-executions-throttled"
  alarm_description   = "Step Functions throttled executions: a burst of events is outrunning the account quota."
  namespace           = "AWS/States"
  metric_name         = "ExecutionThrottled"
  dimensions          = { StateMachineArn = var.state_machine_arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# --- One screen for the loop -------------------------------------------------
resource "aws_cloudwatch_dashboard" "loop" {
  dashboard_name = "${local.p}-loop"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Execuções do laço"
          region = var.region
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", var.state_machine_arn, { label = "iniciadas" }],
            [".", "ExecutionsSucceeded", ".", ".", { label = "concluídas" }],
            [".", "ExecutionsFailed", ".", ".", { label = "escaladas (FAILED por projeto)" }],
            [".", "ExecutionsTimedOut", ".", ".", { label = "timeout" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Duração das Lambdas do laço (p50 · p90)"
          region = var.region
          period = 300
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", var.function_names["analyze"], { stat = "p50", label = "Analyze p50" }],
            ["...", { stat = "p90", label = "Analyze p90" }],
            ["AWS/Lambda", "Duration", "FunctionName", var.function_names["execute"], { stat = "p50", label = "Execute p50" }],
            ["...", { stat = "p90", label = "Execute p90" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Entrada e filas de falha"
          region = var.region
          period = 300
          metrics = [
            ["AWS/Events", "MatchedEvents", "RuleName", var.rule_name, { stat = "Sum", label = "eventos detectados" }],
            [".", "FailedInvocations", ".", ".", { stat = "Sum", label = "detecções perdidas" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.intake_dlq_name, { stat = "Maximum", label = "detecções na fila de intake" }],
            ["...", var.stream_dlq_name, { stat = "Maximum", label = "DLQ dos streams" }],
            ["...", var.escalation_dlq_name, { stat = "Maximum", label = "DLQ das escalações" }],
            # Not alarmed: the alarm would travel through the channel that is stuck.
            ["...", var.alarm_queue_name, { stat = "Maximum", label = "alarmes ainda sem issue" }],
          ]
        }
      },
      {
        type = "alarm", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "Alarmes do laço"
          alarms = concat(
            [aws_cloudwatch_metric_alarm.intake_failed.arn,
              aws_cloudwatch_metric_alarm.scheduler_target_errors.arn,
              aws_cloudwatch_metric_alarm.scheduler_dropped.arn,
              aws_cloudwatch_metric_alarm.exception_sync_failures.arn,
              aws_cloudwatch_metric_alarm.stream_dlq.arn,
              aws_cloudwatch_metric_alarm.sfn_timed_out.arn,
            aws_cloudwatch_metric_alarm.sfn_throttled.arn],
          [for a in aws_cloudwatch_metric_alarm.lambda_errors : a.arn])
        }
      },
    ]
  })
}

output "alarm_names" {
  value = concat(
    [aws_cloudwatch_metric_alarm.intake_failed.alarm_name,
      aws_cloudwatch_metric_alarm.scheduler_target_errors.alarm_name,
      aws_cloudwatch_metric_alarm.scheduler_dropped.alarm_name,
      aws_cloudwatch_metric_alarm.exception_sync_failures.alarm_name,
      aws_cloudwatch_metric_alarm.stream_dlq.alarm_name,
      aws_cloudwatch_metric_alarm.sfn_timed_out.alarm_name,
    aws_cloudwatch_metric_alarm.sfn_throttled.alarm_name],
  [for a in aws_cloudwatch_metric_alarm.lambda_errors : a.alarm_name])
}
output "dashboard_name" { value = aws_cloudwatch_dashboard.loop.dashboard_name }
