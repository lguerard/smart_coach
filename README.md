# Smart Sport

Your own AI sports coach, running on your own machine, built on the
health data your phone already collects.

Every morning it sends you one short message: how recovered your body
is, exactly what tonight's workout should look like, and what to eat
today. No subscription, no cloud service holding your health data —
just you, your numbers, and a plan.

## What it does

- **Reads your real data.** Workouts and sleep come straight from
  your Garmin account; steps, heart rate, weight, body fat and
  nutrition flow in from Android's Health Connect — whatever your
  watch and apps already record.
- **Judges your readiness.** Each day gets a green, yellow or red
  status from your sleep, resting heart rate and recent training
  load. Three red days in a row force an easier week — the coach
  protects you from yourself.
- **Adapts your workout.** Each session type (treadmill, lower body,
  upper body, calisthenics) has its own level that rises and falls
  with your readiness, so tonight's numbers always match today's
  body.
- **Talks like a coach.** Claude turns the numbers into a short,
  personal morning message, pushed to your phone and written into
  tonight's calendar event.
- **Shows the big picture.** A dashboard you can pin to your home
  screen tracks progress, trends, sessions and achievements — and
  lets you tweak tonight's plan with one tap.

## How it works

1. Overnight, your phone backs up its Health Connect data to Google
   Drive — automatically, like it already does.
2. Early morning, Smart Sport asks Garmin for your latest workouts
   and sleep, pulls the Drive backup for the rest, and updates its
   local database.
3. It scores your recovery, picks tonight's training level, and
   computes the day's calorie and macro budget.
4. Claude phrases it all as one coaching message — delivered by
   push notification, calendar event and dashboard before you're
   out of bed.

Everything runs self-hosted in two small Docker containers. Several
people can share one deployment, each with their own data, plan,
calendar and notifications.

## Get started

Setup, architecture and operations live in the
[technical guide](docs/TECHNICAL.md).
