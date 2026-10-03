'use strict';
const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');

function encrypt(input, random) {
  const source = fs.readFileSync(input.module_path, 'utf8');
  if (crypto.createHash('sha256').update(source).digest('hex') !==
      'a0aa85b2fde9cfee03cca7b7dc73a048bd31f43c239748473b89171a7db5e3e8') {
    throw new Error('Source integrity check failed');
  }
  if (!/^[0-9a-f]{32}$/.test(input.digest) || typeof input.user_agent !== 'string') {
    throw new Error('Invalid signing input');
  }
  const context = vm.createContext({
    input, out: {}, random,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
  });
  vm.runInContext(`
    window=globalThis;self=globalThis;
    document={toString:()=>"[object HTMLDocument]"};
    navigator={userAgent:input.user_agent,toString:()=>"[object Navigator]"};
    location={href:"https://zhuanlan.zhihu.com/",toString:()=>"https://zhuanlan.zhihu.com/"};
    history={toString:()=>"[object History]"};screen={toString:()=>"[object Screen]"};
    if(random)Math.random=random;
    (${source.replace(/^1514:/, '')})({},out,id=>{
      if(id===74185)return {_:value=>typeof value};
      throw Error('Unknown dependency');
    });
    if(out.XL!=="3.0")throw Error('Unsupported version');
    signature=out.ZP(input.digest);
  `, context, {timeout: 3000});
  return context.signature;
}

module.exports = {encrypt};
if (require.main === module) {
  try {
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    process.stdout.write(JSON.stringify({version: '3.0', signature: encrypt(input)}));
  } catch (_) {
    // Never print the evaluated source, cookie material, or a VM stack.
    process.stdout.write(JSON.stringify({error: 'signing_failed'}));
    process.exitCode = 1;
  }
}
