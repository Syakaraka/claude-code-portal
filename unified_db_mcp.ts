import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import mysql from "mysql2/promise";

// ================= 强制启用 Oracle Thick 模式 (7.x 正确方案) =================
const oracleLibDir = "/opt/oracle/instantclient";
process.env.LD_LIBRARY_PATH = oracleLibDir;

// 使用 require 绕过所有 ES Module 包装
const oracledb: any = require("oracledb");

try {
  // 7.x 版本中，启用 Thick 模式的方法是 initOracleClient
  if (typeof oracledb.initOracleClient === 'function') {
    oracledb.initOracleClient({ libDir: oracleLibDir });
    console.error(`[INFO] Oracle Thick mode enabled via initOracleClient(). libDir: ${oracleLibDir}`);
  } else {
    console.error(`[ERROR] Cannot find initOracleClient. oracledb version: ${oracledb.version}`);
  }
} catch (error: any) {
  // 如果已经初始化过会报错，忽略即可
  if (!error.message.includes('already been initialized')) {
    console.error(`[WARN] Failed to init Oracle Thick mode: ${error.message}`);
  } else {
    console.error(`[INFO] Oracle Thick mode was already initialized.`);
  }
}

// ================= 数据库连接配置 =================
const DB_CONFIGS_STR = process.env.DB_CONFIGS || "{}";
let DB_CONFIGS: Record<string, any>;
try {
  DB_CONFIGS = JSON.parse(DB_CONFIGS_STR);
} catch (e) {
  console.error("[ERROR] DB_CONFIGS 环境变量 JSON 解析失败");
  DB_CONFIGS = {};
}

// ================= 安全检查函数 =================
function isSafeQuery(sql: string): boolean {
  const cleaned = sql.trim().toUpperCase();
  const safePrefixes = ['SELECT', 'SHOW', 'DESCRIBE', 'DESC', 'EXPLAIN', 'WITH'];

  if (!safePrefixes.some(prefix => cleaned.startsWith(prefix))) return false;
  if (cleaned.includes(';')) return false;

  const forbiddenFunctions = ['SLEEP', 'BENCHMARK', 'LOAD_FILE', 'OUTFILE', 'DUMPFILE'];
  for (const func of forbiddenFunctions) {
    if (new RegExp(`\\b${func}\\s*\\(`).test(cleaned)) return false;
  }

  const forbiddenKeywords = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE', 'RENAME', 'GRANT', 'REVOKE', 'LOCK', 'CALL', 'LOAD', 'MERGE', 'EXEC'];
  for (const keyword of forbiddenKeywords) {
    if (new RegExp(`\\b${keyword}\\b`).test(cleaned)) return false;
  }
  return true;
}

// ================= 初始化 MCP Server =================
const server = new McpServer({
  name: "unified-db-mcp",
  version: "1.0.0"
});

// ================= 注册工具 =================
server.tool(
  "execute_sql",
  `
  Execute a read-only SQL query on the specified database.
  ⚠️ Current Environment: DEV (Development).

  Args:
      db_name: The target database name. Available options:
          - 'gr_oracle': Main business database (Oracle). Corresponds to project code path: 'classpath*:mybatis/gr/*.xml'.
          - 'workflow_mysql': Workflow engine database (MySQL). Corresponds to project code path: 'classpath*:mybatis/mysql/*.xml'.
          - 'tenant_mysql': Tenant management database (MySQL). Corresponds to project code path: 'classpath*:mybatis/tenant/*.xml'.
      sql: The read-only SQL query.

  ALLOWED: SELECT, SHOW, DESCRIBE, DESC, EXPLAIN, WITH.
  BLOCKED: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE and other write operations.
  `,
  {
    db_name: z.string().describe("Database name: gr_oracle, workflow_mysql, or tenant_mysql"),
    sql: z.string().describe("The read-only SQL query")
  },
  async ({ db_name, sql }) => {
    if (!DB_CONFIGS[db_name]) {
      return { content: [{ type: "text", text: `⛔ 错误：未找到名为 '${db_name}' 的数据库配置。可用的数据库: ${Object.keys(DB_CONFIGS).join(', ')}` }] };
    }

    if (!isSafeQuery(sql)) {
      return { content: [{ type: "text", text: "⛔ 安全拦截：仅允许只读查询（SELECT/WITH/SHOW/DESCRIBE/EXPLAIN），禁止增删改及DDL操作！" }] };
    }

    const config = DB_CONFIGS[db_name];
    let conn: mysql.Connection | any | null = null;

    try {
      if (config.type === "mysql") {
        conn = await mysql.createConnection({
          host: config.host,
          port: parseInt(config.port),
          user: config.user,
          password: config.password,
          database: config.database
        });

        try {
          await conn.query("SET TRANSACTION READ ONLY");
        } catch (e) { }

        const [rows] = await conn.query(sql);
        if (!rows || rows.length === 0) {
          return { content: [{ type: "text", text: "Query executed successfully, but returned 0 rows." }] };
        }
        return { content: [{ type: "text", text: JSON.stringify(rows, null, 2) }] };

      } else if (config.type === "oracle") {
        conn = await oracledb.getConnection({
          user: config.user,
          password: config.password,
          connectString: `${config.host}:${config.port}/${config.database}`
        });

        await conn.execute("SET TRANSACTION READ ONLY");

        const result = await conn.execute(sql, [], { outFormat: oracledb.OUT_FORMAT_OBJECT });
        if (!result.rows || result.rows.length === 0) {
          return { content: [{ type: "text", text: "Query executed successfully, but returned 0 rows." }] };
        }
        return { content: [{ type: "text", text: JSON.stringify(result.rows, null, 2) }] };
      }
    } catch (error: any) {
      return { content: [{ type: "text", text: `Error executing SQL: ${error.message}` }] };
    } finally {
      if (conn) {
        try { await conn.close(); } catch (e) {}
      }
    }

    return { content: [{ type: "text", text: "Unknown error occurred." }] };
  }
);

// ================= 启动服务 =================
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((error) => {
  console.error("[FATAL] MCP Server 启动失败:", error);
  process.exit(1);
});

