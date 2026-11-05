"""Board HTML to plain text. Synthetic markup only."""

from __future__ import annotations

from careerhunter.htmltext import to_text


def test_escaped_greenhouse_content_becomes_lines_of_text() -> None:
    content = (
        "&lt;h2&gt;About Example Robotics&lt;/h2&gt;&lt;p&gt;We build things.&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;Fluent French&lt;/li&gt;&lt;li&gt;Salary $40,000&lt;/li&gt;&lt;/ul&gt;"
        "&lt;p&gt;Fish &amp;amp; chips&amp;nbsp;Fridays&lt;br/&gt;Next line&lt;/p&gt;"
    )
    assert to_text(content, escaped=True) == (
        "About Example Robotics\nWe build things.\nFluent French\nSalary $40,000\n"
        "Fish & chips Fridays\nNext line"
    )


def test_unescaped_markup_scripts_and_styles() -> None:
    markup = (
        "<div>Pay range <b>$30</b> - <i>$40</i> per hour</div>"
        "<script>var salary = '$1';</script><style>p {color: red}</style>"
        "<p>  spaced   out  </p>"
    )
    assert to_text(markup) == "Pay range $30 - $40 per hour\nspaced out"


def test_text_without_markup_and_empty_input() -> None:
    assert to_text("Plain words only") == "Plain words only"
    assert to_text("") == ""
    assert to_text("<p> </p><br>") == ""
