from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from bookmarker.pipeline import _write_pdf
from bookmarker.toc import TocEntry, _page_value, normalize_levels, toc_page_bookmark


class TocParsingTest(unittest.TestCase):
    def test_numbered_sibling_consensus_fixes_isolated_cross_page_drift(self) -> None:
        titles_and_levels = [
            ("第1章 矩阵代数基础", 1),
            ("1.1 矩阵", 2),
            ("1.2 向量", 1),
            ("1.3 变换", 2),
            ("1.3.1 定义", 3),
            ("本章小结", 2),
            ("第2章 特殊矩阵", 1),
        ]
        entries = [TocEntry(title, index, "arabic", level, 10 if index < 3 else 11,
                            title, 1.0)
                   for index, (title, level) in enumerate(titles_and_levels, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2, 3, 2, 1])

    def test_numbered_children_without_parent_do_not_nest_under_siblings(self) -> None:
        titles = ["第2章 特殊矩阵", "2.8.1 定义", "2.8.2 性质", "2.9 其它"]
        entries = [TocEntry(title, index, "arabic", 3, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2])

    def test_numbering_decides_levels_when_the_toc_is_read_flat(self) -> None:
        titles = ["第一章 基础", "1.1 矩阵", "1.1.1 向量", "1.2 运算", "第二章 应用",
                  "2.8.1 定义", "2.8.2 性质"]
        entries = [TocEntry(title, index, "arabic", 1, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 3, 2, 1, 2, 2])

    def test_numbering_overrides_a_misread_indent_on_the_same_page(self) -> None:
        rows = [("Chapter 5. The Eye", 1), ("5.1 Introduction", 2),
                ("5.2 Special Topic", 1), ("5.3 Closing", 2)]
        entries = [TocEntry(title, index, "arabic", level, 7, title, 1.0)
                   for index, (title, level) in enumerate(rows, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2])

    def test_repeated_section_number_is_a_sibling_not_a_child(self) -> None:
        titles = ["第1章 基础", "1.1 矩阵", "1.1 矩阵（续）", "1.2 向量"]
        entries = [TocEntry(title, index, "arabic", 2, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2])

    def test_repaired_levels_are_written_as_nested_pdf_bookmarks(self) -> None:
        titles_and_model_levels = [
            ("第1章 基础", 1), ("1.10 Kronecker 积", 2),
            ("1.10.1 定义", 3), ("本章小结", 2),
            ("第2章 特殊矩阵", 1), ("2.1 Hermitian 矩阵", 2),
            ("2.8 Fourier 矩阵", 2), ("2.8.1 定义", 3),
            ("2.8.2 计算", 3),
        ]
        entries = normalize_levels([
            TocEntry(title, 1, "arabic", level, 1, title, 1.0, pdf_page=1)
            for title, level in titles_and_model_levels
        ])
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            output = Path(temporary) / "output.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            writer.write(source)
            _write_pdf(source, output, entries)

            actual: list[tuple[str, int]] = []

            def walk(items: list, level: int = 1) -> None:
                for item in items:
                    if isinstance(item, list):
                        walk(item, level + 1)
                    else:
                        actual.append((str(item["/Title"]), level))

            walk(PdfReader(output).outline)
        self.assertEqual(actual, [(title, level) for (title, _), level in zip(
            titles_and_model_levels, [1, 2, 3, 2, 1, 2, 2, 3, 3])])

    def test_unnumbered_chapter_items_are_siblings_of_numbered_sections_in_pdf(self) -> None:
        # Two printed TOC pages: the model can occasionally call an indented
        # unnumbered item level 1, although it repeats at the end of chapters.
        titles_and_visual_levels = [
            ("Chapter 4. Prisms and Mirrors", 1, 7),
            ("4.17 Analysis of Fabrication Errors", 2, 7),
            ("Bibliography", 1, 7),
            ("Chapter 5. The Eye", 1, 8),
            ("5.1 Introduction", 2, 8),
            ("5.4 Defects of the Eye", 2, 8),
            ("Bibliography", 1, 8),
            ("Exercises", 1, 8),
            ("Chapter 6. Stops and Apertures", 1, 9),
            ("6.1 Introduction", 2, 9),
            ("Further Reading", 2, 9),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, source_page, title, 1.0,
                     pdf_page=1)
            for index, (title, level, source_page) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 2, 1, 2, 2, 2, 2, 1, 2, 2])

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            output = Path(temporary) / "output.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            writer.write(source)
            _write_pdf(source, output, entries)

            parented: list[tuple[str, str | None]] = []

            def walk(items: list, parent: str | None = None) -> None:
                current: str | None = None
                for item in items:
                    if isinstance(item, list):
                        walk(item, current)
                    else:
                        current = str(item["/Title"])
                        parented.append((current, parent))

            walk(PdfReader(output).outline)

        self.assertEqual(parented, [
            ("Chapter 4. Prisms and Mirrors", None),
            ("4.17 Analysis of Fabrication Errors", "Chapter 4. Prisms and Mirrors"),
            ("Bibliography", "Chapter 4. Prisms and Mirrors"),
            ("Chapter 5. The Eye", None),
            ("5.1 Introduction", "Chapter 5. The Eye"),
            ("5.4 Defects of the Eye", "Chapter 5. The Eye"),
            ("Bibliography", "Chapter 5. The Eye"),
            ("Exercises", "Chapter 5. The Eye"),
            ("Chapter 6. Stops and Apertures", None),
            ("6.1 Introduction", "Chapter 6. Stops and Apertures"),
            ("Further Reading", "Chapter 6. Stops and Apertures"),
        ])

    def test_standalone_book_level_bibliography_remains_top_level(self) -> None:
        titles_and_levels = [
            ("Preface", 1),
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("Bibliography", 1),
            ("Index", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 1, 2, 1, 1])

    def test_single_unnumbered_endings_follow_sections_before_next_chapter(self) -> None:
        titles_and_visual_levels = [
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("Bibliography", 1),
            ("Further Reading", 1),
            ("Notes", 1),
            ("Chapter 6. Stops and Apertures", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 2, 2, 2, 1])

    def test_part_chapters_and_unnumbered_rows_follow_visual_levels(self) -> None:
        titles_and_visual_levels = [
            ("Part I. Foundations", 1),
            ("Chapter 1. Basic Concepts", 2),
            ("1.1 Introduction", 3),
            ("Further Reading", 3),
            ("Chapter 2. Applications", 2),
            ("2.1 Methods", 3),
            ("Notes", 3),
            ("Part II. Practice", 1),
            ("Chapter 3. Design", 2),
            ("3.1 Examples", 3),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 3, 3, 2, 3, 3, 1, 2, 3])

    def test_unnumbered_row_joins_the_numbered_rows_at_its_indent(self) -> None:
        titles_and_visual_levels = [
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("5.1.1 Additional Detail", 2),
            ("Practical Notes", 2),
            ("5.1.2 Summary", 2),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 3, 3, 3])

    def test_book_level_appendix_is_not_absorbed_into_last_chapter(self) -> None:
        titles_and_visual_levels = [
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("Bibliography", 1),
            ("Appendix A. Tables", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 1, 1])

    def test_chapter_appendices_and_introduction_sections_follow_toc_indent(self) -> None:
        # The layout of 《光学教程》: indented "附录 1.1" rows belong to their
        # chapter, and "0.1" sections belong to the unnumbered "绪论".
        titles_and_visual_levels = [
            ("绪论", 1),
            ("0.1 光学的研究内容和方法", 2),
            ("0.2 光学发展简史", 2),
            ("第1章 光的干涉", 1),
            ("1.1 波动的独立性、叠加性和相干性", 2),
            ("1.10 光的干涉应用举例 牛顿环", 2),
            ("视窗与链接 增透膜与高反射膜", 2),
            ("附录 1.1 振动叠加的三种计算方法", 2),
            ("附录 1.2 简谐波的表达式 复振幅", 2),
            ("习题", 2),
            ("第2章 光的衍射", 1),
            ("2.1 惠更斯-菲涅耳原理", 2),
            ("附录 2.1 夫琅禾费单缝衍射公式的推导", 2),
            ("附录 A 常用物理常量", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 1])

    def test_sections_nest_under_unnumbered_chapter_headings(self) -> None:
        titles_and_visual_levels = [
            ("光的干涉", 1),
            ("1.1 相干性", 2),
            ("1.2 双缝干涉", 2),
            ("阅读材料", 2),
            ("光的衍射", 1),
            ("2.1 单缝衍射", 2),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 2, 2, 1, 2])

    def test_indented_chapter_appendix_is_written_inside_its_chapter(self) -> None:
        rows = [("第1章 光的干涉", 1, 1), ("1.1 相干性", 2, 1),
                ("附录 1.1 振动叠加的三种计算方法", 2, 2), ("第2章 光的衍射", 1, 3)]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0, pdf_page=page)
            for index, (title, level, page) in enumerate(rows, 1)
        ])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "output.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            _write_pdf(source, output, entries)
            outline = PdfReader(output).outline
        self.assertEqual(str(outline[0]["/Title"]), "第1章 光的干涉")
        self.assertEqual([str(item["/Title"]) for item in outline[1]],
                         ["1.1 相干性", "附录 1.1 振动叠加的三种计算方法"])
        self.assertEqual(str(outline[2]["/Title"]), "第2章 光的衍射")

    def test_continuation_page_without_chapter_row_is_realigned(self) -> None:
        # 《光学原理》: the second contents page begins inside chapter 4 with
        # 4.1.5 and 4.2, and every row on it came back one level too shallow.
        rows = [
            (17, "第4章 光学成像的几何理论", 1),
            (17, "4.1 哈密顿特征函数", 2),
            (17, "4.1.1 点特征函数", 3),
            (17, "4.1.2 混合特征函数", 3),
            (17, "4.1.3 角特征函数", 3),
            (17, "4.1.4 旋转折射面的角特征函数近似形式", 3),
            (18, "4.1.5 旋转反射面的角特征函数近似形式", 2),
            (18, "4.2 理想成像", 1),
            (18, "4.2.1 一般定理", 2),
            (18, "4.2.2 麦克斯韦“鱼眼”", 2),
            (18, "4.2.3 面的无像散成像", 2),
            (18, "4.3 具有轴对称的射影变换(直射变换)", 1),
            (18, "4.3.1 一般公式", 2),
            (19, "第5章 像差的几何理论", 1),
            (19, "5.1 程差函数和像差函数", 2),
            (19, "5.1.1 基本概念", 3),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, page, title, 1.0)
            for index, (page, title, level) in enumerate(rows, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 3, 3, 3, 3, 3, 2, 3, 3, 3, 2, 3, 1, 2, 3])

    def test_numbering_decides_levels_across_pages(self) -> None:
        flat = [(1, "第一章 基础", 1), (1, "1.1 矩阵", 1), (1, "1.2 向量", 1),
                (2, "1.3 运算", 1), (2, "1.4 变换", 1), (2, "第二章 应用", 1)]
        anchored = [(1, "第1章 绪论", 1), (1, "1.1 背景", 2), (1, "1.1.1 起源", 3),
                    (2, "第2章 方法", 1), (2, "2.1 模型", 1), (2, "2.2 算法", 1)]
        for rows, expected in ((flat, [1, 2, 2, 2, 2, 1]), (anchored, [1, 2, 3, 1, 2, 2])):
            with self.subTest(rows=rows[0][1]):
                entries = normalize_levels([
                    TocEntry(title, index, "arabic", level, page, title, 1.0)
                    for index, (page, title, level) in enumerate(rows, 1)
                ])
                self.assertEqual([entry.level for entry in entries], expected)

    def test_section_sign_sections_own_their_subsections(self) -> None:
        # 《光学》(赵凯华、钟锡华): "第二章" holds "§ 1" and "§ 2", whose
        # subsections are 1.1 and 2.1.  The second contents page begins inside
        # chapter 1 and came back one level too shallow.
        rows = [
            (9, "绪论", 1, 1), (9, "1. 光的本性", 2, 2), (9, "2. 光源和光谱", 2, 2),
            (9, "第一章 几何光学", 1, 1), (9, "§1 几何光学基本定律", 2, 2),
            (9, "1.1 几何光学三定律", 3, 3), (9, "1.4 光的可逆性原理", 3, 3),
            (9, "思考题", 3, 3), (9, "习题", 3, 3), (9, "§2 惠更斯原理", 2, 2),
            (9, "2.1 波的几何描述", 3, 3), (9, "习题", 3, 3),
            (10, "§ 11 光度学基本概念", 1, 2), (10, "11.1 辐射能通量和光通量", 2, 3),
            (10, "11.5 光度学单位的定义", 2, 3), (10, "习题", 2, 3),
            (10, "* § 12 像的亮度、照度和主观亮度", 1, 2), (10, "12.1 像的亮度", 2, 3),
            (10, "12.3 主观亮度", 2, 3), (10, "思考题", 2, 3), (10, "习题", 2, 3),
            (10, "第二章 波动光学基本原理", 1, 1), (10, "§ 1 定态光波与复振幅描述", 2, 2),
            (10, "1.1 波动概述", 3, 3), (10, "1.5 强度的复振幅表示", 3, 3),
            (10, "思考题", 3, 3), (10, "习题", 3, 3), (10, "§ 2 波前", 2, 2),
            (10, "2.1 波前的概念", 3, 3), (10, "* 2.4 高斯光束", 3, 3),
            (10, "思考题", 3, 3), (10, "§ 3 波的叠加和波的干涉", 2, 2),
            (10, "3.1 波的叠加原理", 3, 3),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, page, title, 1.0)
            for index, (page, title, level, _) in enumerate(rows, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [expected for *_, expected in rows])

    def test_section_sign_rows_nest_even_when_the_model_reads_them_flat(self) -> None:
        # 《新概念物理教程·光学》: §1. to §5. inside "第一章", then the
        # chapter's summary and exercises beside the sections.
        rows = [
            ("第一章 光和光的传播", 1), ("§1. 光和光学", 2), ("1.1 光的本性", 3),
            ("1.3 光学的研究对象、分支与应用", 3), ("§2. 光的几何光学传播规律", 2),
            ("2.1 几何光学三定律", 3), ("2.4 光路的可逆性原理", 3), ("§3. 惠更斯原理", 2),
            ("3.1 波的几何描述", 3), ("§5. 光度学基本概念", 2), ("5.5 光度学单位的定义", 3),
            ("本章提要", 2), ("思考题", 2), ("习题", 2), ("第二章 几何光学成像", 1),
        ]
        expected = [level for _, level in rows]
        for reported in ([level for _, level in rows],
                         [1 if title.startswith("第") else 2 for title, _ in rows]):
            with self.subTest(reported=reported):
                entries = normalize_levels([
                    TocEntry(title, index, "arabic", level, 1, title, 1.0)
                    for index, ((title, _), level) in enumerate(zip(rows, reported), 1)
                ])
                self.assertEqual([entry.level for entry in entries], expected)

    def test_arabic_items_under_roman_headings_follow_their_heading(self) -> None:
        # Born & Wolf, Principles of Optics: "Appendices" holds roman-numbered
        # appendices, each with arabic-numbered items one level deeper.
        rows = [
            ("XV Optics of crystals", 1),
            ("15.6 Interference with crystal plates", 2),
            ("15.6.2 Interference figures from absorbing crystal plates", 3),
            ("(a) Uniaxial crystals", 4),
            ("15.6.3 Dichroic polarizers", 3),
            ("Appendices", 1),
            ("I The Calculus of variations", 2),
            ("1 Euler's equations as necessary conditions for an extremum", 3),
            ("2 Hilbert's independence integral and the Hamilton-Jacobi equation", 3),
            ("12 Example II: Mechanics of material points", 3),
            ("II Light optics, electron optics and wave mechanics", 2),
            ("1 The Hamiltonian analogy in elementary form", 3),
            ("4 The application of optical principles to electron optics", 2),
            ("III Asymptotic approximations to integrals", 2),
            ("1 The method of steepest descent", 3),
            ("Author index", 1),
            ("Subject index", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 34, title, 1.0)
            for index, (title, level) in enumerate(rows, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 3, 4, 3, 1, 2, 3, 3, 3, 2, 3, 3, 2, 3, 1, 1])

    def test_arabic_chapters_stay_top_level_without_roman_heading(self) -> None:
        rows = [("Preface", 1), ("1 Introduction", 1), ("1.1 Scope", 2),
                ("2 Methods", 2), ("2.1 Setup", 2), ("Index", 1)]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(rows, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 1, 2, 1, 2, 1])

    def test_contents_page_bookmark_follows_the_language_of_the_toc(self) -> None:
        chinese = [TocEntry(title, page, "arabic", 1, 5, title, 1.0)
                   for page, title in enumerate(["绪论", "第1章 光的干涉", "Appendix A"], 1)]
        bookmark = toc_page_bookmark(chinese, 5)
        self.assertEqual((bookmark.title, bookmark.level, bookmark.pdf_page), ("目录", 1, 5))
        english = [TocEntry(title, page, "arabic", 1, 3, title, 1.0)
                   for page, title in enumerate(["Preface", "Chapter 1 Optics", "附录"], 1)]
        self.assertEqual(toc_page_bookmark(english, 3).title, "Contents")

    def test_printed_page_label_accepts_parenthesized_arabic_and_roman(self) -> None:
        self.assertEqual(_page_value("（１）"), ("arabic", 1))
        self.assertEqual(_page_value("iv"), ("roman", 4))
        self.assertIsNone(_page_value("0"))


if __name__ == "__main__":
    unittest.main()
