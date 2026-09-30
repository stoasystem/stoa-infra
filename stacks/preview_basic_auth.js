import cf from 'cloudfront';

// Basic Auth for a preview distribution (CloudFront Functions, JS runtime 2.0).
//
// The expected credential is base64("user:password"), stored under the key
// "basic-auth" in the key value store associated with this function. CDK
// creates the store empty; a person writes the value after deploy.
//
// Anything short of an exact match is a 401: no store, no key, an empty or
// non-string value, a missing header, a different scheme, a different value.
// Nothing here logs, and the response never echoes what was sent or expected.

var KVS_KEY = 'basic-auth';

function deny() {
  return {
    statusCode: 401,
    statusDescription: 'Unauthorized',
    headers: {
      'www-authenticate': { value: 'Basic realm="STOA preview", charset="UTF-8"' },
      'x-robots-tag': { value: 'noindex' },
      'cache-control': { value: 'no-store' },
    },
  };
}

async function handler(event) {
  var request = event.request;
  var expected;
  try {
    expected = await cf.kvs('__KVS_ID__').get(KVS_KEY);
  } catch (e) {
    return deny();
  }
  if (typeof expected !== 'string' || expected.length === 0) {
    return deny();
  }
  var header = request.headers.authorization;
  if (!header || header.value !== 'Basic ' + expected) {
    return deny();
  }
  return request;
}
