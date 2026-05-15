import { startExaProxy } from "../exa_proxy/server.js";
import { pool } from "../db/pool.js";

const PORT = parseInt(process.env.EXA_PROXY_PORT ?? "8765", 10);

const { close } = await startExaProxy({ port: PORT });

const shutdown = async () => {
  console.log("[exa-proxy] shutting down");
  await close();
  await pool.end();
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
