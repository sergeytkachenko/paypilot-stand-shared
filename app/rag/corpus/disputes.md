# Payment dispute rules

## Reason codes and chargeback windows
Each reason code has a window in days from the transaction date. A dispute opened after the window closes is not eligible.

| Reason code | Window (days) |
|---|---|
| fraud_card_not_present | 120 |
| goods_not_received | 90 |
| duplicate_charge | 60 |
| service_not_rendered | 90 |
| unauthorized_debit | 56 |

## Eligibility conditions
A dispute is eligible only if all of the following hold: the reason code is
valid; the transaction status is settled; the transaction date is within the
reason code's window relative to the current date; and no customer-level
restriction applies (for example, an account under compliance review cannot
open new disputes until the review concludes).

## Escalation on blocked disputes
When a dispute is blocked by a customer-level restriction, the case must be
escalated to a human agent.
