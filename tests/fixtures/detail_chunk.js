// The announcement dialog's lazily loaded chunk: it carries the file helper.
webpackJsonp.push([["chunk-detail"], {"63a1": function (module, exports) {
  exports.a = function (id) {
    return fetch('/px-settlement-infpubmeex/fileService/download?fileId=' + id)
      .then(r => r.arrayBuffer()).then(bytes => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([bytes])); a.download = id + '.pdf';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
      });
  };
}}]);
