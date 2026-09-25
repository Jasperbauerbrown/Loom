"""html_to_markdown / html_to_text: dependency-free HTML conversion for tool output."""

from __future__ import annotations

from loom.tools.native.html_text import html_to_markdown, html_to_text


# ---------------------------------------------------------------------------------
# noise dropping
# ---------------------------------------------------------------------------------
def test_script_and_style_are_dropped_entirely():
    html = "<p>keep</p><script>var x = 1 < 2;</script><style>p { color: red; }</style>"
    assert "var x" not in html_to_markdown(html)
    assert "color: red" not in html_to_markdown(html)
    assert "keep" in html_to_markdown(html)


def test_navigational_chrome_is_dropped():
    html = "<nav>Home | About</nav><header>Site</header><main><p>Article body</p></main><footer>copy</footer><aside>ads</aside>"
    md = html_to_markdown(html)
    assert "Home" not in md and "Site" not in md and "copy" not in md and "ads" not in md
    assert "Article body" in md


def test_form_iframe_and_svg_are_dropped():
    html = "<p>before</p><form><input name='q'></form><iframe src='x'></iframe><svg><circle /></svg><p>after</p>"
    md = html_to_markdown(html)
    assert "before" in md and "after" in md
    assert "input" not in md and "circle" not in md


# ---------------------------------------------------------------------------------
# headings and paragraphs
# ---------------------------------------------------------------------------------
def test_headings_markdown():
    md = html_to_markdown("<h1>Title</h1><h3>Sub</h3>")
    assert "# Title" in md
    assert "### Sub" in md


def test_headings_and_paragraphs_are_blank_line_separated_in_text():
    text = html_to_text("<h1>Title</h1><p>One</p><p>Two</p>")
    assert text == "Title\n\nOne\n\nTwo"


def test_paragraph_markdown_has_blank_line_around_it():
    md = html_to_markdown("<p>First</p><p>Second</p>")
    assert md == "First\n\nSecond"


# ---------------------------------------------------------------------------------
# lists
# ---------------------------------------------------------------------------------
def test_unordered_list_markdown():
    md = html_to_markdown("<ul><li>a</li><li>b</li></ul>")
    assert "- a" in md and "- b" in md


def test_ordered_list_numbers_items():
    md = html_to_markdown("<ol><li>first</li><li>second</li><li>third</li></ol>")
    assert "1. first" in md
    assert "2. second" in md
    assert "3. third" in md


def test_nested_list_is_indented_two_spaces_per_level():
    html = "<ul><li>outer<ul><li>inner</li></ul></li></ul>"
    md = html_to_markdown(html)
    lines = [ln for ln in md.split("\n") if ln.strip()]
    inner = [ln for ln in lines if "inner" in ln][0]
    assert inner.startswith("  - ") or inner.startswith("  ")
    assert inner.count("inner") == 1


def test_list_items_one_per_line_in_text():
    text = html_to_text("<ul><li>a</li><li>b</li></ul>")
    assert "a" in text.split("\n")
    assert "b" in text.split("\n")


# ---------------------------------------------------------------------------------
# links and images
# ---------------------------------------------------------------------------------
def test_link_markdown():
    md = html_to_markdown('<a href="https://x.test">click here</a>')
    assert md == "[click here](https://x.test)"


def test_link_in_text_mode_is_just_the_text():
    text = html_to_text('<a href="https://x.test">click here</a>')
    assert text == "click here"


def test_link_with_no_href_degrades_to_text():
    md = html_to_markdown("<a>bare text</a>")
    assert md == "bare text"
    assert "[" not in md


def test_link_with_empty_text_emits_nothing_noisy():
    md = html_to_markdown('<a href="https://x.test"></a>')
    assert "[](" not in md


def test_javascript_href_renders_just_the_text():
    md = html_to_markdown('<a href="javascript:void(0)">do thing</a>')
    assert md == "do thing"
    assert "javascript:" not in md


def test_image_markdown():
    md = html_to_markdown('<img src="a.png" alt="a cat">')
    assert md == "![a cat](a.png)"


def test_image_text_mode_is_just_the_alt():
    text = html_to_text('<img src="a.png" alt="a cat">')
    assert text == "a cat"


# ---------------------------------------------------------------------------------
# emphasis, code, pre
# ---------------------------------------------------------------------------------
def test_strong_and_em_markdown():
    md = html_to_markdown("<p><strong>bold</strong> and <em>italic</em> and <b>b</b> and <i>i</i></p>")
    assert "**bold**" in md
    assert "*italic*" in md
    assert "**b**" in md
    assert "*i*" in md


def test_inline_code_markdown():
    md = html_to_markdown("<p>run <code>ls -la</code> now</p>")
    assert "`ls -la`" in md


def test_pre_block_is_fenced_and_unescaped():
    html = "<pre><code>def f(x):\n    return x * 2\n</code></pre>"
    md = html_to_markdown(html)
    assert "```" in md
    assert "def f(x):" in md
    assert "    return x * 2" in md
    assert "**" not in md  # no emphasis markers introduced inside the fence


def test_pre_preserves_whitespace():
    html = "<pre>a    b\n  c</pre>"
    md = html_to_markdown(html)
    assert "a    b" in md
    assert "  c" in md


# ---------------------------------------------------------------------------------
# blockquote, hr, br
# ---------------------------------------------------------------------------------
def test_blockquote_prefixes_lines():
    md = html_to_markdown("<blockquote>quoted text</blockquote>")
    assert "> quoted text" in md


def test_hr_is_a_rule():
    md = html_to_markdown("<p>a</p><hr><p>b</p>")
    assert "---" in md


def test_br_is_a_line_break():
    md = html_to_markdown("<p>line one<br>line two</p>")
    assert "line one" in md and "line two" in md
    assert md.count("\n") >= 1


# ---------------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------------
def test_table_with_header_row():
    html = "<table><thead><tr><th>Name</th><th>Age</th></tr></thead><tbody><tr><td>Alice</td><td>30</td></tr></tbody></table>"
    md = html_to_markdown(html)
    lines = [ln for ln in md.split("\n") if ln.strip()]
    assert lines[0] == "| Name | Age |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| Alice | 30 |"


def test_table_without_header_row_still_emits_pipe_rows():
    html = "<table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>"
    md = html_to_markdown(html)
    lines = [ln for ln in md.split("\n") if ln.strip()]
    assert lines[0] == "| 1 | 2 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 3 | 4 |"


def test_table_cell_pipe_is_escaped_and_newlines_collapsed():
    html = "<table><tr><td>a | b</td><td>line1<br>line2</td></tr></table>"
    md = html_to_markdown(html)
    assert "a \\| b" in md
    assert "\n" not in md.split("\n")[0]


# ---------------------------------------------------------------------------------
# entities and whitespace
# ---------------------------------------------------------------------------------
def test_entities_are_resolved():
    text = html_to_text("<p>Tom &amp; Jerry &#39;s &nbsp; place</p>")
    assert "Tom & Jerry" in text
    assert "&amp;" not in text and "&#39;" not in text and "&nbsp;" not in text


def test_whitespace_is_collapsed_inside_inline_text():
    text = html_to_text("<p>a\n\n   b\t\tc</p>")
    assert text == "a b c"


def test_output_never_has_more_than_two_consecutive_newlines():
    html = "<p>a</p>" + "<br><br><br><br>" + "<p>b</p>"
    result = html_to_markdown(html)
    assert "\n\n\n" not in result


def test_result_is_stripped_of_leading_and_trailing_whitespace():
    text = html_to_text("   <p>content</p>   ")
    assert text == text.strip()
    assert text == "content"


# ---------------------------------------------------------------------------------
# robustness: malformed input never raises
# ---------------------------------------------------------------------------------
def test_empty_string_does_not_raise():
    assert html_to_markdown("") == ""
    assert html_to_text("") == ""


def test_unclosed_tags_do_not_raise():
    result = html_to_markdown("<p>opened but never closed <div><span>more")
    assert "opened but never closed" in result
    assert "more" in result


def test_stray_close_tags_do_not_raise():
    result = html_to_text("</div>text</span></p><p>after</close>")
    assert "text" in result
    assert "after" in result


def test_fragment_with_no_html_wrapper_does_not_raise():
    result = html_to_markdown("just text, no tags at all")
    assert result == "just text, no tags at all"


def test_deeply_nested_input_does_not_raise_or_blow_the_stack():
    depth = 20000
    html = "<div>" * depth + "deep" + "</div>" * depth
    result = html_to_text(html)
    assert "deep" in result


def test_garbage_bytes_and_bad_markup_do_not_raise():
    result = html_to_markdown("<<<>>><p attr='unterminated>text<<< &notarealentity;")
    assert isinstance(result, str)


def test_none_like_or_odd_but_valid_str_inputs_do_not_raise():
    for candidate in ["<", ">", "<>", "&", "&amp", "<!--", "<!-- unterminated comment", "<![CDATA[x"]:
        assert isinstance(html_to_markdown(candidate), str)
        assert isinstance(html_to_text(candidate), str)


def test_angle_brackets_in_prose_do_not_cost_the_page_its_structure():
    """`a < b`, or a docs page about `<div>`, leaves angle brackets in the output. That
    is prose, not a failed parse: falling back to the regex strip here would delete the
    sentence it appears in."""
    html = "<h1>Docs</h1><p>Use &lt;div&gt; for a block.</p><p>Also if a &lt; b and b &gt; c.</p>"
    text = html_to_text(html)
    assert "<div>" in text and "a < b and b > c" in text
    assert text.count("\n\n") == 2  # heading and both paragraphs still separated
    assert "# Docs" in html_to_markdown(html)
