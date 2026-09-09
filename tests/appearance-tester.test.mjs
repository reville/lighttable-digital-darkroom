import test from 'node:test';
import assert from 'node:assert/strict';
import {createAppearanceSettings} from '../web/appearance-tester.js';

// Independent window state with queued cross-window messages, as in WebKit.
function windows({storageAvailable = true, broadcastAvailable = true} = {}) {
  let saved = null;
  const clients = [], channels = [], messages = [];
  return {
    flush() { while (messages.length) messages.shift()(); },
    open() {
      const listeners = new Map();
      const notify = () => clients.filter(c => c !== listeners).forEach(c =>
        messages.push(() => c.get('storage')?.({key:'lighttable.appearance-test.v1'})));
      const win = {
        localStorage: {
          getItem() { if (!storageAvailable) throw Error('storage denied'); return saved; },
          setItem(key, value) { if (!storageAvailable) throw Error('storage denied'); saved = value; notify(); },
          removeItem() { if (!storageAvailable) throw Error('storage denied'); saved = null; notify(); },
        },
        BroadcastChannel: class {
          constructor() { if (!broadcastAvailable) throw Error('unavailable'); channels.push(this); }
          postMessage(data) {
            for (const channel of channels.filter(c => c !== this && !c.closed)) {
              const copy = structuredClone(data);
              messages.push(() => { if (!channel.closed) channel.onmessage?.({data:copy}); });
            }
          }
          close() { this.closed = true; }
        },
        addEventListener: (name, fn) => listeners.set(name, fn),
        removeEventListener: name => listeners.delete(name),
      };
      clients.push(listeners);
      return createAppearanceSettings({win});
    },
  };
}

for (const options of [{}, {storageAvailable:false}, {broadcastAvailable:false}]) {
  test(`a separate tester updates the editor and retains settings after reopening ${JSON.stringify(options)}`, () => {
    const session = windows(options), editor = session.open(), tester = session.open();
    session.flush();
    tester.set({color:'#33CC99', borders:false});
    session.flush();
    assert.deepEqual(editor.get(), {color:'#33cc99', borders:false});
    tester.close();
    const reopened = session.open();
    session.flush();
    assert.deepEqual(reopened.get(), editor.get());
    reopened.reset();
    session.flush();
    assert.deepEqual(editor.get(), {color:null, borders:true});
    assert.deepEqual(tester.get(), {color:'#33cc99', borders:false});
    editor.close(); reopened.close();
  });
}

test('an opening-window snapshot cannot undo an immediate color change', () => {
  const session = windows({storageAvailable:false}), editor = session.open();
  editor.set({color:'#445566', borders:true});
  const tester = session.open();
  tester.set({color:'#998877', borders:false});
  session.flush();
  assert.deepEqual(tester.get(), {color:'#998877', borders:false});
  assert.deepEqual(editor.get(), tester.get());
  editor.close(); tester.close();
});
