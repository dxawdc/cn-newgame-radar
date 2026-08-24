// 仅观察游戏中心公开页面请求；不修改请求，不输出查询串和敏感字段。
Java.perform(function () {
  const targetHost = /(gamecenter|game\.dbankcloud|knights\.mi|game\.heytapmobi)/i;
  const targetPath = /(pageapi\/v1\/detail|frame\/v1\/(?:dap\/asses|ass)(?:$|\/)|assembly\/listByStrategy|contentapi\/gamecenter\/(?:setting\/page|page\/subscribe)|gbClientApi|gamecenter\.do|keywordlist|new.?game|beta|appoint|reserve)/i;
  const sensitive = /(token|cookie|auth|account|user|device|udid|imei|oaid|open.?id|android.?id|serial|phone|sign|secret|password|session|trace|appLst|package|pName|sha(?:256)?|installed|duplicate|localPlayed|file)/i;

  function clean(value, depth) {
    if (depth > 6) return "[depth]";
    if (Array.isArray(value)) return value.slice(0, 100).map(v => clean(v, depth + 1));
    if (value && typeof value === "object") {
      const output = {};
      Object.keys(value).forEach(function (key) {
        if (!sensitive.test(key)) output[key] = clean(value[key], depth + 1);
      });
      return output;
    }
    if (typeof value === "string" && value.length > 160) return value.slice(0, 160) + "…";
    return value;
  }

  function emit(url, method, body, headers) {
    try {
      const rawUrl = String(url);
      const pieces = rawUrl.split("?", 2);
      const noQuery = pieces[0];
      if (!targetHost.test(noQuery) || !targetPath.test(noQuery)) return;
      const publicQuery = {};
      if (pieces.length > 1) {
        pieces[1].split("&").forEach(function (pair) {
          const equals = pair.indexOf("=");
          const rawKey = equals >= 0 ? pair.slice(0, equals) : pair;
          const rawValue = equals >= 0 ? pair.slice(equals + 1) : "";
          let key = rawKey;
          let value = rawValue;
          try { key = decodeURIComponent(rawKey.replace(/\+/g, " ")); } catch (_) {}
          try { value = decodeURIComponent(rawValue.replace(/\+/g, " ")); } catch (_) {}
          if (!sensitive.test(key)) publicQuery[key] = value.length > 160 ? value.slice(0, 160) + "…" : value;
        });
      }
      let publicBody = null;
      if (body && body.length <= 20000) {
        try { publicBody = clean(JSON.parse(body), 0); }
        catch (_) { publicBody = "[非JSON正文已丢弃]"; }
      }
      send({ url: noQuery, method: String(method || "GET"), query: publicQuery, headers: headers || {}, body: publicBody });
    } catch (_) {}
  }

  try {
    const RequestBuilder = Java.use("okhttp3.Request$Builder");
    const build = RequestBuilder.build.overload();
    build.implementation = function () {
      const request = build.call(this);
      let bodyText = null;
      const publicHeaders = {};
      try {
        const body = request.body();
        if (body) {
          const Buffer = Java.use("okio.Buffer");
          const buffer = Buffer.$new();
          body.writeTo(buffer);
          bodyText = buffer.readUtf8();
        }
      } catch (_) {}
      try {
        const headerNames = request.headers().names().toArray();
        for (let i = 0; i < headerNames.length; i += 1) {
          const name = String(headerNames[i]);
          if (sensitive.test(name)) continue;
          if (/^(accept|content-type|user-agent|x-.*(?:app|client|version|language|locale|market|region|country|channel|os)|(?:app|client|version|language|locale|market|region|country|channel|os))/i.test(name)) {
            const value = String(request.header(name) || "");
            publicHeaders[name] = value.length > 160 ? value.slice(0, 160) + "…" : value;
          }
        }
      } catch (_) {}
      emit(request.url().toString(), request.method(), bodyText, publicHeaders);
      return request;
    };
    send({ status: "okhttp3 hook ready" });
  } catch (error) {
    send({ status: "okhttp3 hook unavailable", error: String(error) });
  }

  // 荣耀会先把公开页面请求序列化成 JSON 字符串；在这一层观察可以避免 OkHttp 具体实现差异。
  try {
    const BaseReqImpl = Java.use("com.hihonor.gamecenter.base_net.core.BaseReqImpl");
    const buildReqBody = BaseReqImpl.buildReqBody.overload(
      "com.hihonor.gamecenter.base_net.base.BaseRequestInfo"
    );
    buildReqBody.implementation = function (requestInfo) {
      const result = buildReqBody.call(this, requestInfo);
      try {
        const requestClass = requestInfo ? String(requestInfo.$className || requestInfo.getClass().getName()) : "";
        if (/GetCMSAssemblyAppReq|PageAssembly/i.test(requestClass)) {
          let publicBody = null;
          try { publicBody = clean(JSON.parse(String(result)), 0); }
          catch (_) { publicBody = "[非JSON正文已丢弃]"; }
          send({ status: "honor public request body", requestClass: requestClass, body: publicBody });
        }
      } catch (_) {}
      return result;
    };
    send({ status: "honor serializer hook ready" });
  } catch (error) {
    send({ status: "honor serializer hook unavailable", error: String(error) });
  }
});
