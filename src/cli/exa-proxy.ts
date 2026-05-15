import { startExaProxy } from "../exa_proxy/server.js";
import { pool } from "../db/pool.js";

const PORT = parseInt(process.env.EXA_PROXY_PORT ?? "8765", 10);
const HOST = process.env.EXA_PROXY_HOST ?? "127.0.0.1";

const { close } = await startExaProxy({ port: PORT, host: HOST });

const shutdown = async () => {
  console.log("[exa-proxy] shutting down");
  await close();
  await pool.end();
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
