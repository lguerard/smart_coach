// Dump Garmin's exercise catalog (category -> exercise names, in the
// UPPER_SNAKE form the Connect workout API takes) from the official FIT
// SDK, for scripts/build_garmin_names.py:
//   npm install @garmin/fitsdk
//   node scripts/garmin_catalog.mjs > /tmp/garmin_catalog.json
import { Profile } from '@garmin/fitsdk';

const snake = (s) => s
  .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
  .replace(/([A-Z])([A-Z][a-z])/g, '$1_$2')
  .toUpperCase();
const out = {};
for (const category of Object.values(Profile.types.exerciseCategory)) {
  if (typeof category !== 'string') continue;
  const names = Profile.types[`${category}ExerciseName`] || {};
  out[snake(category)] = Object.values(names)
    .filter((v) => typeof v === 'string').map(snake);
}
console.log(JSON.stringify(out));
