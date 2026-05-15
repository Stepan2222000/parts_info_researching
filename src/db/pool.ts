import pg from "pg";
import { PARTS_RESEARCH_DATABASE_URL } from "../config.js";

export const pool = new pg.Pool({ connectionString: PARTS_RESEARCH_DATABASE_URL });
