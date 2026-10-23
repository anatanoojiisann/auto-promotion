"""Read observed X editor and public text without rendered paragraph artifacts."""

EDITOR_TEXT_JS = r"""element => {
  function read(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    if (node.tagName === 'IMG') return node.getAttribute('alt') || '';
    if (node.tagName === 'BR') return '\n';
    return Array.from(node.childNodes, read).join('');
  }
  const blocks = Array.from(element.querySelectorAll('[data-block="true"]'));
  if (!blocks.length) {
    const lines = [];
    let inline = '';
    for (const node of element.childNodes) {
      if (node.nodeType === 1 && /^(DIV|P)$/.test(node.tagName)) {
        if (inline || !lines.length) {lines.push(inline); inline = '';}
        const text = read(node);
        lines.push(text === '\n' && !node.textContent ? '' : text);
      } else inline += read(node);
    }
    if (inline || !lines.length) lines.push(inline);
    return lines.join('\n');
  }
  return blocks.map(block => {
    const text = read(block);
    return text === '\n' && !block.textContent ? '' : text;
  }).join('\n');
}"""

TWEET_TEXT_JS = r"""element => {
  function read(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    if (node.tagName === 'BR') return '\n';
    if (node.tagName === 'IMG') return node.getAttribute('alt') || '';
    if (node.tagName === 'A') {
      const url = node.getAttribute('data-expanded-url') || node.getAttribute('title');
      if (url && /^https?:\/\//.test(url)) return url;
    }
    return Array.from(node.childNodes, read).join('');
  }
  return read(element);
}"""
