#!/usr/bin/env node
import { main } from '../lib/cli.mjs';

main(process.argv.slice(2)).catch(error => {
  console.error(`LightTable: ${error.message}`);
  process.exitCode = Number.isInteger(error.exitCode) ? error.exitCode : 1;
});
