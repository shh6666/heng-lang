const vscode = require('vscode');
const { LanguageClient, ServerOptions, TransportKind } = require('vscode-languageclient/node');

let client;

function activate(context) {
  const config = vscode.workspace.getConfiguration('heng');
  if (config.get('lsp.enabled') === false) {
    return;
  }

  const pythonPath = config.get('pythonPath') || 'python3';
  // LSP server 入口：python3 -m heng.lsp
  const serverOptions = {
    command: pythonPath,
    args: ['-m', 'heng.lsp'],
    options: {
      env: { ...process.env },
      // 工作区根目录加入 PYTHONPATH，确保能 import heng 包
    },
  };

  // 若工作区包含 heng-lang 目录，把它加入 PYTHONPATH
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length > 0) {
    const root = folders[0].uri.fsPath;
    serverOptions.options.env.PYTHONPATH = [
      root,
      `${root}/heng-lang`,
      process.env.PYTHONPATH || '',
    ].filter(Boolean).join(':');
  }

  const clientOptions = {
    documentSelector: [{ scheme: 'file', language: 'heng' }],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.hng'),
    },
  };

  client = new LanguageClient('heng', '衡语语言服务', serverOptions, clientOptions);
  client.start();
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
